# Shopify Insights

A small multi-brand Shopify Admin API CLI focused on products. One tool, one config
file per brand/store under `config/stores/`. Pulls products to CSV, reads/writes
product details (including Shopify's structured "category metafields"), runs
arbitrary GraphQL queries, and can run mutations against any connected store.

Built for a team sharing this repo: each brand gets its own gitignored credentials file,
so adding, rotating, or removing one brand never touches another's config or causes a
merge conflict.

See [CLAUDE.md](CLAUDE.md) for the internal architecture (how category metafields
resolve to metaobjects, the deterministic suggestion engine, batch-fix conservatism,
etc.) if you're modifying `category_metafields.py`.

## Layout

```
client.py                        shared Admin API client (OAuth, GraphQL, pagination, throttle retry)
category_metafields.py           category-metafield read/write engine (see CLAUDE.md)
main.py                          CLI entry point (see Commands below)
config/
  stores/
    _template.json.example       committed template — copy this to add a brand
    <Brand>.json                 one file per brand, gitignored (real secrets live here)
queries/
  products.graphql               built-in query used by the `products` command
  product_get.graphql            single-product read used by `get-product`
  product_update.graphql         generic productUpdate mutation used by `update-product`
  product_update_title.graphql   example mutation (productUpdate) run via `query`
output/
  <Brand>/                       CSV/JSON exports per brand (gitignored, contains PII)
```

## Setup

Requires Python 3.10+.

```
python -m venv .venv
.venv\Scripts\activate      # or source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
```

## Adding a new brand/store

Each brand needs its own Shopify **custom app** (created in that store's admin under
**Settings → Apps and sales channels → Develop apps**, or via the Partner Dashboard).

1. **Copy the template** to a new file named after the brand:
   ```
   copy config\stores\_template.json.example config\stores\<Brand>.json
   ```
   (`cp` on macOS/Linux). Fill in `shop`, `client_id`, `client_secret` from that store's
   custom app. Use a short, consistent brand name — it's what you pass to every command
   as `--store <Brand>`, and it's what output files get grouped under.

2. **Set the app's Allowed redirection URL(s)** in the app's configuration to exactly:
   ```
   http://localhost:8080/callback
   ```
   This must match `REDIRECT_URI` in `client.py`. A mismatch here is one of the ways
   Shopify's install link comes back as "invalid".

3. **Check the app's configured access scopes.** The scope requested in the OAuth URL
   (`SCOPES` in `client.py`) must be a **subset** of what's actually enabled on the app —
   asking for one scope the app doesn't have enabled makes Shopify reject the *entire*
   install link as invalid (not just that scope). If you hit that error, compare
   `SCOPES` against the app's Configuration page and trim to match. Currently `SCOPES`
   is: `read_channels,read_files,write_files,write_inventory,read_inventory,
   read_metaobject_definitions,write_metaobject_definitions,read_metaobjects,
   write_metaobjects,read_product_feeds,write_product_feeds,read_product_listings,
   write_product_listings,read_products,write_products,read_reports,
   unauthenticated_read_product_inventory,unauthenticated_read_product_listings,
   unauthenticated_read_product_tags`.

4. **Get the authorize URL:**
   ```
   python main.py authorize --store <Brand> --show-url
   ```

5. **Open that URL** while logged into the store admin **as the store owner** (or a
   staff account with matching elevated permissions). Non-owner staff accounts get
   `invalid_request: Your account does not have permission to grant the requested
   access` if the requested scope touches anything sensitive (payments, billing, plan
   management, etc.) — log in as the owner if you see that.

6. **Catch the redirect code.** Either:
   - Run `python main.py catch-code` *before* opening the URL — it starts a local
     server on port 8080 and prints the code the moment Shopify redirects back, or
   - Approve manually and copy the `?code=...` value out of the browser's address bar
     yourself.

7. **Save the code and exchange it for a token.** Paste the code into that brand's
   `auth_code` field in `config/stores/<Brand>.json`, then run:
   ```
   python main.py authorize --store <Brand>
   ```
   On success this saves the access token into that brand's file and clears
   `auth_code`. The store is now connected — `client.py`'s `graphql()`/`paginate()`
   helpers will use that token for every request.

8. **Verify it's picked up:**
   ```
   python main.py list-stores
   ```
   Lists every brand with a config file and whether it's authorized yet, without
   printing any secrets.

### Troubleshooting install errors

| Error | Meaning | Fix |
|---|---|---|
| `invalid_request: ... does not have permission to grant the requested access` | Logged-in account isn't the store owner (or lacks a specific elevated permission the requested scope needs) | Log in as the store owner and re-approve |
| "The installation link for this app is invalid" | A scope in the request isn't enabled on the app's own configuration, or the redirect URL doesn't match what's allowlisted | Compare `SCOPES` in `client.py` against the app's Configuration page; verify the redirect URL is set exactly to `http://localhost:8080/callback` |
| `No config for store 'X' at config/stores/X.json` | No file for that brand yet | Copy `_template.json.example` to `config/stores/X.json` and fill it in |
| `Store 'X' has no token yet` | Brand's `token` field is empty — never authorized, or token was never exchanged | Run through the connect flow above |

## Commands

```
python main.py list-stores                                # list configured brands + auth status (no secrets printed)
python main.py authorize --store <Brand> [--show-url]      # get/refresh a token
python main.py catch-code [--port 8080]                    # local server to catch the OAuth redirect code
python main.py products  --store <Brand> [--output path]   # export all products to CSV (default: output/<Brand>/products.csv)
python main.py get-product --store <Brand> --id <gid> [--output path]     # read one product's full details
python main.py update-product --store <Brand> --id <gid>                 # write product fields
  [--title ...] [--vendor ...] [--product-type ...] [--status ACTIVE|ARCHIVED|DRAFT]
  [--description <html>] [--tags "a,b,c"] [--output path]
python main.py category-options --store <Brand> --id <gid>               # list a product's category attributes + allowed values
python main.py suggest-category-metafields --store <Brand> --id <gid> [--apply]   # keyword-rule guesses for missing category metafields
python main.py set-category-metafields --store <Brand> --id <gid> --set "Attribute=Value1,Value2"  # write specific category metafields (repeatable --set)
python main.py batch-fix-categories --store <Brand> [--limit 50] [--apply]   # batch: fix grossly mismatched categories + apply rule-based metafield suggestions
python main.py query --store <Brand> --file queries/whatever.graphql [--variables '{"...": "..."}'] [--output path]
```

`get-product`/`update-product` cover the common read/write cases for a single product
directly (no hand-written GraphQL needed). Reach for `query` with a custom `.graphql`
file for anything else — bulk edits, variants, images, etc.

### Category metafields

Shopify's "Category metafields" (the Color/Size/Neckline/Target gender/... panel shown
in the admin once a product has a taxonomy Category assigned) are structured
`shopify.*` namespace metafields backed by system metaobjects — not plain text. See
[CLAUDE.md](CLAUDE.md) for the full resolution chain and the platform gotchas it works
around (duplicate metaobjects, inconsistent plus-size naming, scoped definitions, etc.).

Three complementary tools:
- `category-options` — list a product's category attributes and every allowed value,
  useful before hand-picking values for `set-category-metafields`.
- `suggest-category-metafields` — a small deterministic keyword-rule engine (checks
  title/description/tags/variant options for unambiguous signals like "Herren"→Male,
  "Rundhalsausschnitt"→Round/Crew neckline, variant sizes→Size). Only fills in what it's
  confident about; everything else it lists as needing a manual/AI read of the product.
  `--apply` writes it; without that flag it's a dry run.
- `set-category-metafields` — write specific attribute/value pairs once you (or an
  agent reading the product) have decided them, e.g.:
  ```
  python main.py set-category-metafields --store TomHollinger --id gid://shopify/Product/123 \
    --set "Target gender=Male" --set "Sleeve length type=Long"
  ```
  Value names must match `category-options`' output (short size codes like "S" also
  match "Small (S)"-style names). An attribute with no metafield definition enabled yet
  for the shop needs one enabled first (via the Shopify admin, or
  `standardMetafieldDefinitionEnable`) before it's scriptable.

`batch-fix-categories` runs both the category-mismatch check and the suggestion engine
across the first N products in a store in one pass, with a shared cache so it doesn't
re-fetch the same shop-wide metaobject/definition data per product. It's deliberately
conservative about category reassignment — see CLAUDE.md — and always writes a JSON
report to `output/<Brand>/category_batch_report.json` (or `--output`) so you can see
exactly what happened, including anything that got rejected or left unresolved.

`query` runs any `.graphql` file against the store — this is how ad-hoc reads and
mutations get done without adding a dedicated subcommand for each one. Example (used
to strip "50% RABATT" sale copy out of a live product title):

```
python main.py query --store TomHollinger --file queries/product_update_title.graphql \
  --variables '{"id":"gid://shopify/Product/123","title":"Clean Title"}' \
  --output output/TomHollinger/update_result.json
```

## Notes

- `config/stores/<Brand>.json` holds live secrets (client secret + access token) and is
  gitignored per-file — never commit it. `_template.json.example` is the only file in
  that folder that ships in the repo.
- `output/<Brand>/` is gitignored too (exported product data).
- API version is pinned in `client.py` (`API_VERSION`) — bump it there when needed.
- Adding a brand never touches another brand's file — safe for multiple people to work
  on different brands in the same repo at once.
