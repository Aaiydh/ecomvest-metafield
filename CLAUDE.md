# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A multi-brand Shopify Admin API CLI (`main.py` + `client.py`), plus a standalone
subsystem (`category_metafields.py`) for reading/writing Shopify's structured
"category metafields" (Color, Size, Neckline, Target gender, etc. — the panel
shown in the Shopify admin once a product has a taxonomy Category assigned).
Pure Python, no framework, no test suite, no build/lint step — `pip install -r
requirements.txt` (just `requests`) is the entire setup.

## Running it

```
python main.py <command> --store <Brand> ...
python main.py --help                          # full command list
python main.py list-stores                     # see configured brands + auth status
```

There is no local/mock mode — every command hits the real Shopify Admin API for
whichever `--store` you pass. There's nothing to build or lint; the only way to
verify a change is to run the relevant command against a real connected store
(or `python -c "import ast; ast.parse(open('main.py').read())"` for a quick
syntax check before that).

## Architecture

**`client.py`** — `ShopifyClient`: OAuth (`authorize_url`/`exchange_code`),
`graphql(query, variables)`, and `paginate(query, variables, data_path)` for
walking any `edges`/`pageInfo` connection. `graphql()` auto-retries on
`THROTTLED` GraphQL errors with exponential backoff — expect that behavior
(and preserve it) whenever code adds new bulk/loop API usage. `SCOPES` is the
OAuth scope string requested on connect; changing it only affects *new*
authorizations, not already-connected stores.

**Per-brand config** — `config/stores/<Brand>.json`, one file per brand,
gitignored (`config/stores/*.json` in `.gitignore`); `_template.json.example`
is the only tracked file in that directory. `client.get_store_config()`/
`list_store_names()` read this directory directly — there is no central
registry file. `--store <Brand>` throughout the CLI must match a filename
there exactly (case-sensitive).

**`main.py`** — thin argparse layer; each `cmd_*` function is a single
command's implementation, reading a `.graphql` file from `queries/` where one
exists, or delegating to `category_metafields.py` for the category-metafield
commands. Exports (`products`, `category_batch_report.json`, etc.) default to
`output/<Brand>/<file>`, also gitignored.

**`category_metafields.py`** — the non-obvious part of this codebase. Shopify
"category metafields" are `list.metaobject_reference` fields under the
`shopify` namespace (e.g. `shopify.target-gender`), not plain text. Setting
one requires a resolution chain:

1. A product's assigned taxonomy **Category** (`product.category`) exposes a
   fixed set of `TaxonomyChoiceListAttribute`s (e.g. "Target gender",
   "Neckline") — these vary by category (Sweaters ≠ Sneakers) and are fetched
   per-product via `PRODUCT_CATEGORY_QUERY`.
2. Each attribute's human display name maps to a metafield `key` (e.g.
   "Target gender" → `target-gender`) via `metafieldDefinitions(namespace:
   "shopify")` — **a shop only has definitions for attributes it has actually
   used before**; a brand-new attribute has no key yet (see gotcha below).
3. Each allowed *value* (e.g. "Male") is itself a taxonomy value that must be
   resolved to a **metaobject GID** of type `shopify--<key>` before it can be
   written — `MetaobjectCache.find_metaobject()` does this by matching the
   metaobject's `taxonomy_reference` (or `color_taxonomy_reference`/
   `pattern_taxonomy_reference`) field.

`MetafieldCache` exists because steps 2–3 are shop-wide/type-wide, not
per-product — reuse one cache instance across a batch instead of creating a
new one per product, or every product refetches the same metaobject lists.

Known platform gotchas baked into this logic — don't "fix" these away without
re-reading why they're there:
- **"Color" silently covers "Pattern" too** — Shopify's admin merges the
  separate Pattern taxonomy attribute into the same `color-pattern`
  metaobject/field, so `_write_category_metafields` checks both attributes'
  value lists when resolving a "Color" input.
- **Duplicate metaobjects can map to the same taxonomy value** (e.g. a custom
  German "Blau" alongside the standard "Blue", both referencing the same
  taxonomy Blue value) — resolution prefers an exact label match to what was
  requested rather than the first one found in an arbitrary order.
- **Plus-size naming is inconsistent in Shopify's own taxonomy** — some
  entries read "Triple extra large (XXXL)", others "Four extra large (4XL)".
  `_normalize_size_code()` collapses both styles before comparing.
- **Enabling a standard metafield definition doesn't seed its metaobjects** —
  `standardMetafieldDefinitionEnable` can succeed while the corresponding
  `shopify--<key>` metaobjects still don't exist yet (observed with "Outsole
  material"); that attribute stays unwritable via API until someone picks a
  value for it once through the Admin UI.
- **A metafield write can fail with "Owner subtype does not match the
  metafield definition's constraints"** for non-apparel products (seen on
  smartwatches/calendars) even though the category's attribute list appears
  to allow it — this is a real Shopify-side scope restriction, not a bug in
  this code. `_write_category_metafields` resolves/submits every attribute
  independently so one such rejection never discards a product's other valid
  writes (see `resolution_errors`/`userErrors` in its return value).

**`suggest_category_metafields()`** is a deliberately small, deterministic
keyword-rule engine (title/description/tags/variant options → high-confidence
values only) — currently clothing-vocabulary only (gender, neckline, sleeve
length, pattern, size). It is meant to be extended per product vertical (shoe
terms, bag terms, etc.) rather than made "smarter"/fuzzy; anything it isn't
sure about it must leave in the `unresolved` list rather than guess, since
that list is the handoff point to a manual/AI read of the product.

**`category_is_gross_mismatch()`/`find_correct_category()`/`batch_fix()`** —
the category-correction pass is deliberately conservative: it only reassigns
a category when the *current* one is in a completely wrong top-level
department for the product's `productType` (e.g. a shoe filed under Sporting
Goods), via `VERTICAL_TOP_BRANCH`. It never overrides a category that's
already in the right department, even if its leaf name doesn't literally
match `productType`'s last word — that could be a more specific, still-correct
assignment. Don't loosen this without understanding that tradeoff.

## Queries

`.graphql` files in `queries/` are the built-ins used by dedicated commands
(`products.graphql`, `product_get.graphql`, `product_update.graphql`,
`product_update_title.graphql` — the last is also a worked example for the
generic `query` command). For anything not covered by a dedicated command,
`python main.py query --store <Brand> --file <path.graphql>` runs any
ad-hoc `.graphql` file (read or mutation) without adding new CLI surface.
