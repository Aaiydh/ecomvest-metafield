"""Shopify Insights CLI entry point."""
import argparse
import csv
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import client as shopify
import category_metafields

QUERIES_DIR = Path(__file__).parent / "queries"
OUTPUT_DIR = Path(__file__).parent / "output"


def _store_output(store: str, filename: str) -> str:
    return str(OUTPUT_DIR / store / filename)


def _write_output(data, output_path, fieldnames=None):
    if output_path is None:
        print(json.dumps(data, indent=2))
        return
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix.lower() == ".csv":
        rows = data if isinstance(data, list) else [data]
        with out.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames or (list(rows[0].keys()) if rows else []))
            writer.writeheader()
            writer.writerows(rows)
        print(f"Wrote {len(rows)} row(s) to {out}")
    else:
        out.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"Wrote output to {out}")


def cmd_authorize(args):
    if args.show_url:
        print(shopify.authorize_url(args.store))
        return
    shopify.exchange_code(args.store)
    print(f"Authorized '{args.store}'. Token saved to config/stores/{args.store}.json.")


def cmd_list_stores(args):
    names = shopify.list_store_names()
    if not names:
        print(
            "No stores configured yet. Copy config/stores/_template.json.example to "
            "config/stores/<Brand>.json and fill it in."
        )
        return
    for name in names:
        cfg = shopify.get_store_config(name)
        status = "connected" if cfg.get("token") else "not authorized"
        print(f"{name}: {cfg.get('shop')} ({status})")


def cmd_catch_code(args):
    code_holder = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            qs = parse_qs(urlparse(self.path).query)
            code = qs.get("code", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            if code:
                code_holder["code"] = code
                self.wfile.write(b"Code received. You can close this tab.")
            else:
                self.wfile.write(b"No code found in the request.")

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("localhost", args.port), Handler)
    print(f"Listening on http://localhost:{args.port}/callback ... waiting for redirect.")
    while "code" not in code_holder:
        server.handle_request()
    print(f"code={code_holder['code']}")


def cmd_products(args):
    query = (QUERIES_DIR / "products.graphql").read_text(encoding="utf-8")
    cl = shopify.ShopifyClient(args.store)
    rows = []
    for node in cl.paginate(query, {}, ["products"]):
        price_range = node.get("priceRangeV2") or {}
        min_price = (price_range.get("minVariantPrice") or {}).get("amount")
        max_price = (price_range.get("maxVariantPrice") or {}).get("amount")
        variants = [e["node"] for e in (node.get("variants") or {}).get("edges", [])]
        rows.append({
            "id": node["id"],
            "title": node.get("title"),
            "handle": node.get("handle"),
            "vendor": node.get("vendor"),
            "product_type": node.get("productType"),
            "status": node.get("status"),
            "tags": ", ".join(node.get("tags", [])),
            "total_inventory": node.get("totalInventory"),
            "min_price": min_price,
            "max_price": max_price,
            "variant_count": len(variants),
            "skus": ", ".join(v.get("sku") or "" for v in variants),
            "created_at": node.get("createdAt"),
            "updated_at": node.get("updatedAt"),
        })
    _write_output(rows, args.output or _store_output(args.store, "products.csv"))


def cmd_get_product(args):
    query = (QUERIES_DIR / "product_get.graphql").read_text(encoding="utf-8")
    cl = shopify.ShopifyClient(args.store)
    data = cl.graphql(query, {"id": args.id})
    product = data.get("product")
    if product is None:
        print(f"No product found for id {args.id}", file=sys.stderr)
        sys.exit(1)
    _write_output(product, args.output)


def cmd_update_product(args):
    input_fields = {"id": args.id}
    if args.title is not None:
        input_fields["title"] = args.title
    if args.vendor is not None:
        input_fields["vendor"] = args.vendor
    if args.product_type is not None:
        input_fields["productType"] = args.product_type
    if args.status is not None:
        input_fields["status"] = args.status
    if args.description is not None:
        input_fields["descriptionHtml"] = args.description
    if args.tags is not None:
        input_fields["tags"] = [t.strip() for t in args.tags.split(",") if t.strip()]

    if len(input_fields) == 1:
        print(
            "Nothing to update - pass at least one of "
            "--title/--vendor/--product-type/--status/--description/--tags.",
            file=sys.stderr,
        )
        sys.exit(1)

    query = (QUERIES_DIR / "product_update.graphql").read_text(encoding="utf-8")
    cl = shopify.ShopifyClient(args.store)
    data = cl.graphql(query, {"input": input_fields})
    result = data["productUpdate"]
    if result["userErrors"]:
        print(json.dumps(result["userErrors"], indent=2), file=sys.stderr)
        sys.exit(1)
    _write_output(result["product"], args.output)


def cmd_category_options(args):
    cl = shopify.ShopifyClient(args.store)
    product = category_metafields.get_product_category(cl, args.id)
    category = product.get("category")
    if category is None:
        print("This product has no category assigned.", file=sys.stderr)
        sys.exit(1)
    print(f"{product['title']}  ({category['fullName']})\n")
    for node in category["attributes"]["nodes"]:
        if node.get("__typename") != "TaxonomyChoiceListAttribute":
            continue
        values = ", ".join(v["name"] for v in node["values"]["nodes"])
        print(f"{node['name']}: {values}")


def cmd_suggest_category_metafields(args):
    cl = shopify.ShopifyClient(args.store)
    product = category_metafields.get_product_category(cl, args.id)
    if product.get("category") is None:
        print("This product has no category assigned.", file=sys.stderr)
        sys.exit(1)

    suggestions, unresolved = category_metafields.suggest_category_metafields(product)

    if suggestions:
        print("Rule-based suggestions:")
        for name, values in suggestions.items():
            print(f"  {name}: {', '.join(values)}")
    else:
        print("No rule-based suggestions found.")

    if unresolved:
        print("\nNeeds a manual/AI read (no confident keyword match):")
        for name in unresolved:
            print(f"  {name}")

    if args.apply:
        if not suggestions:
            print("\nNothing to apply.")
            return
        result = category_metafields.set_category_metafields(cl, args.id, suggestions)
        print("\nApplied:")
        for mf in result["metafields"]:
            print(f"  {mf['namespace']}.{mf['key']} = {mf['value']}")


def cmd_set_category_metafields(args):
    values = {}
    for item in args.set or []:
        if "=" not in item:
            print(f"Invalid --set '{item}', expected \"Attribute=Value1,Value2\"", file=sys.stderr)
            sys.exit(1)
        name, raw_values = item.split("=", 1)
        values[name.strip()] = [v.strip() for v in raw_values.split(",") if v.strip()]

    if not values:
        print('Pass at least one --set "Attribute=Value1,Value2".', file=sys.stderr)
        sys.exit(1)

    cl = shopify.ShopifyClient(args.store)
    result = category_metafields.set_category_metafields(cl, args.id, values)
    for mf in result["metafields"]:
        print(f"{mf['namespace']}.{mf['key']} = {mf['value']}")

    problems = result.get("resolution_errors", []) + result.get("userErrors", [])
    if problems:
        print("Not applied:", file=sys.stderr)
        for p in problems:
            label = p.get("attribute") or "/".join(str(x) for x in p.get("field", []))
            print(f"  {label}: {p.get('message')}", file=sys.stderr)
        if not result["metafields"]:
            sys.exit(1)


def cmd_batch_fix_categories(args):
    cl = shopify.ShopifyClient(args.store)
    results = category_metafields.batch_fix(cl, args.limit, apply=args.apply)

    fixed = [r for r in results if r.get("category_fixed")]
    mismatched_unresolved = [r for r in results if r.get("category_mismatch_unresolved") or r.get("category_would_fix_to")]
    with_metafields = [r for r in results if r.get("metafields_applied")]
    partial_failures = [r for r in results if r.get("metafields_failed")]
    errors = [r for r in results if r.get("error")]

    mode = "Applied" if args.apply else "Dry run (no writes) -"
    print(f"{mode} {len(results)} products checked.")
    print(f"  Category fixed: {len(fixed)}")
    print(f"  Category mismatch found but not auto-resolved: {len(mismatched_unresolved)}")
    print(f"  Products with metafield suggestions: {len(with_metafields)}")
    print(f"  Products with a rejected/unresolved field (others in the same product still applied): {len(partial_failures)}")
    print(f"  Errors (product-level, nothing written): {len(errors)}")

    if fixed:
        print("\nCategory fixes:")
        for r in fixed:
            print(f"  {r['id']}  {r['title']!r}: {r['category_before']} -> {r['category_after']}")

    if mismatched_unresolved:
        print("\nMismatches needing manual review:")
        for r in mismatched_unresolved:
            reason = r.get("category_mismatch_unresolved") or r.get("category_would_fix_to")
            print(f"  {r['id']}  {r['title']!r}: {r['category_before']} ({reason})")

    if partial_failures:
        print("\nRejected/unresolved fields:")
        for r in partial_failures:
            for p in r["metafields_failed"]:
                label = p.get("attribute") or "/".join(str(x) for x in p.get("field", []))
                print(f"  {r['id']}  {r['title']!r}  {label}: {p.get('message')}")

    if errors:
        print("\nErrors:")
        for r in errors:
            print(f"  {r['id']}  {r.get('title')!r}: {r['error']}")

    _write_output(results, args.output or _store_output(args.store, "category_batch_report.json"))


def cmd_query(args):
    query = Path(args.file).read_text(encoding="utf-8")
    variables = json.loads(args.variables) if args.variables else {}
    cl = shopify.ShopifyClient(args.store)
    data = cl.graphql(query, variables)
    _write_output(data, args.output)


def main():
    parser = argparse.ArgumentParser(prog="main.py", description="Shopify Insights CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("authorize", help="Get/refresh an access token for a store")
    p.add_argument("--store", required=True)
    p.add_argument("--show-url", action="store_true")
    p.set_defaults(func=cmd_authorize)

    p = sub.add_parser("catch-code", help="Local server to catch the OAuth redirect code")
    p.add_argument("--port", type=int, default=8080)
    p.set_defaults(func=cmd_catch_code)

    p = sub.add_parser("list-stores", help="List configured stores/brands and their connection status")
    p.set_defaults(func=cmd_list_stores)

    p = sub.add_parser("products", help="Export all products to CSV")
    p.add_argument("--store", required=True)
    p.add_argument("--output")
    p.set_defaults(func=cmd_products)

    p = sub.add_parser("get-product", help="Read a single product's full details")
    p.add_argument("--store", required=True)
    p.add_argument("--id", required=True, help="Product GID, e.g. gid://shopify/Product/123")
    p.add_argument("--output")
    p.set_defaults(func=cmd_get_product)

    p = sub.add_parser(
        "update-product",
        help="Write product fields (title/vendor/type/status/description/tags)",
    )
    p.add_argument("--store", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--title")
    p.add_argument("--vendor")
    p.add_argument("--product-type", dest="product_type")
    p.add_argument("--status", choices=["ACTIVE", "ARCHIVED", "DRAFT"])
    p.add_argument("--description", help="HTML body for the product description")
    p.add_argument("--tags", help="Comma-separated tag list; replaces all existing tags")
    p.add_argument("--output")
    p.set_defaults(func=cmd_update_product)

    p = sub.add_parser(
        "category-options",
        help="Show a product's category attribute names and their allowed values",
    )
    p.add_argument("--store", required=True)
    p.add_argument("--id", required=True)
    p.set_defaults(func=cmd_category_options)

    p = sub.add_parser(
        "suggest-category-metafields",
        help="Keyword-rule suggestions for a product's missing category metafields",
    )
    p.add_argument("--store", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--apply", action="store_true", help="Write the suggested values instead of just printing them")
    p.set_defaults(func=cmd_suggest_category_metafields)

    p = sub.add_parser(
        "set-category-metafields",
        help='Write specific category metafield values, e.g. --set "Target gender=Male"',
    )
    p.add_argument("--store", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--set", action="append", help='"Attribute=Value1,Value2" (repeatable)')
    p.set_defaults(func=cmd_set_category_metafields)

    p = sub.add_parser(
        "batch-fix-categories",
        help="Batch: fix grossly mismatched categories + apply deterministic category metafield suggestions",
    )
    p.add_argument("--store", required=True)
    p.add_argument("--limit", type=int, default=50, help="Number of products to process (default: 50)")
    p.add_argument("--apply", action="store_true", help="Write changes; without this it's a dry run")
    p.add_argument("--output", help="Where to save the JSON report (default: output/<Brand>/category_batch_report.json)")
    p.set_defaults(func=cmd_batch_fix_categories)

    p = sub.add_parser("query", help="Run an arbitrary .graphql file (read or mutation)")
    p.add_argument("--store", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--variables", help="JSON object of GraphQL variables")
    p.add_argument("--output")
    p.set_defaults(func=cmd_query)

    args = parser.parse_args()
    try:
        args.func(args)
    except (FileNotFoundError, KeyError, ValueError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
