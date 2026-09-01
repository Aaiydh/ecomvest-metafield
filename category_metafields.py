"""Read/write Shopify's standard "category metafields" (namespace 'shopify'),
the structured attributes (Color, Size, Neckline, Target gender, etc.) that
Shopify derives from a product's assigned taxonomy Category.
"""
import json
import re

PRODUCT_CATEGORY_QUERY = """
query ProductCategory($id: ID!) {
  product(id: $id) {
    id
    title
    productType
    descriptionHtml
    tags
    category {
      id
      name
      fullName
      attributes(first: 50) {
        nodes {
          __typename
          ... on TaxonomyChoiceListAttribute {
            id
            name
            values(first: 250) {
              nodes { id name }
            }
          }
        }
      }
    }
    variants(first: 100) {
      edges {
        node {
          selectedOptions { name value }
        }
      }
    }
  }
}
"""

METAFIELD_DEFINITIONS_QUERY = """
query ShopifyCategoryMetafieldDefinitions($cursor: String) {
  metafieldDefinitions(first: 100, after: $cursor, ownerType: PRODUCT, namespace: "shopify") {
    edges { node { key name } }
    pageInfo { hasNextPage endCursor }
  }
}
"""

METAOBJECTS_BY_TYPE_QUERY = """
query MetaobjectsByType($type: String!, $cursor: String) {
  metaobjects(type: $type, first: 100, after: $cursor) {
    edges {
      node {
        id
        fields { key value }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

METAFIELDS_SET_MUTATION = """
mutation SetMetafields($metafields: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $metafields) {
    metafields { namespace key value }
    userErrors { field message }
  }
}
"""

TAXONOMY_SEARCH_QUERY = """
query TaxonomySearch($search: String!) {
  taxonomy {
    categories(search: $search, first: 20) {
      nodes {
        id
        name
        fullName
        isLeaf
      }
    }
  }
}
"""

CATEGORY_UPDATE_MUTATION = """
mutation SetProductCategory($input: ProductInput!) {
  productUpdate(input: $input) {
    product {
      id
      category { id fullName }
    }
    userErrors { field message }
  }
}
"""

PRODUCT_IDS_QUERY = """
query ProductIds($cursor: String) {
  products(first: 50, after: $cursor) {
    edges { node { id } }
    pageInfo { hasNextPage endCursor }
  }
}
"""


def get_product_category(cl, product_id: str) -> dict:
    data = cl.graphql(PRODUCT_CATEGORY_QUERY, {"id": product_id})
    product = data.get("product")
    if product is None:
        raise ValueError(f"No product found for id {product_id}")
    return product


def get_attribute_name_to_key(cl) -> dict:
    """Map a taxonomy attribute's display name (e.g. 'Target gender') to the
    metafield key (e.g. 'target-gender') for every standard definition this
    shop already has enabled."""
    return {
        node["name"]: node["key"]
        for node in cl.paginate(METAFIELD_DEFINITIONS_QUERY, {}, ["metafieldDefinitions"])
    }


class MetafieldCache:
    """Per-run cache so a batch of many products doesn't refetch the same
    metafield-definition map, metaobject lists, or taxonomy search results
    over and over - those are shop-wide/type-wide, not per-product."""

    def __init__(self, cl):
        self.cl = cl
        self._name_to_key = None
        self._metaobjects_by_type = {}
        self._taxonomy_search = {}

    def attribute_name_to_key(self) -> dict:
        if self._name_to_key is None:
            self._name_to_key = get_attribute_name_to_key(self.cl)
        return self._name_to_key

    def _metaobjects_for_type(self, metaobject_type: str):
        if metaobject_type not in self._metaobjects_by_type:
            entries = []
            for node in self.cl.paginate(METAOBJECTS_BY_TYPE_QUERY, {"type": metaobject_type}, ["metaobjects"]):
                fields = {f["key"]: f["value"] for f in node["fields"]}
                entries.append((node["id"], fields))
            self._metaobjects_by_type[metaobject_type] = entries
        return self._metaobjects_by_type[metaobject_type]

    def find_metaobject(self, metaobject_type: str, taxonomy_value_id: str, preferred_label: str = None):
        """Find the metaobject of `metaobject_type` whose taxonomy reference
        field (plain, color, or pattern) points at `taxonomy_value_id`. Shops
        can have several metaobjects mapped to the same taxonomy value (e.g. a
        custom German "Blau" alongside the standard "Blue") - when that
        happens, prefer the one whose label matches `preferred_label`
        exactly, and only fall back to the first match found if none does."""
        matches = []
        for metaobject_id, fields in self._metaobjects_for_type(metaobject_type):
            for ref_key in ("taxonomy_reference", "color_taxonomy_reference", "pattern_taxonomy_reference"):
                raw = fields.get(ref_key)
                if not raw:
                    continue
                ids = json.loads(raw) if raw.startswith("[") else [raw]
                if taxonomy_value_id in ids:
                    matches.append((metaobject_id, fields.get("label", "")))
                    break

        if not matches:
            return None
        if preferred_label:
            for metaobject_id, label in matches:
                if label.strip().lower() == preferred_label.strip().lower():
                    return metaobject_id
        return matches[0][0]

    def search_category(self, search_term: str) -> list:
        if search_term not in self._taxonomy_search:
            data = self.cl.graphql(TAXONOMY_SEARCH_QUERY, {"search": search_term})
            self._taxonomy_search[search_term] = data["taxonomy"]["categories"]["nodes"]
        return self._taxonomy_search[search_term]


def _choice_list_attributes(category: dict) -> dict:
    return {
        node["name"]: {v["name"]: v["id"] for v in node["values"]["nodes"]}
        for node in category["attributes"]["nodes"]
        if node.get("__typename") == "TaxonomyChoiceListAttribute"
    }


def _normalize_size_code(code: str) -> str:
    """Shopify's own taxonomy is inconsistent about plus-size naming - some
    entries say "Triple extra large (XXXL)", others say "Four extra large
    (4XL)". Collapse both styles to the same repeated-X form so "3XL" and
    "XXXL" (or "4XL" and "XXXXL") compare equal."""
    code = code.strip().upper()
    match = re.fullmatch(r"(\d+)X(S|L)", code)
    if match:
        count, suffix = match.groups()
        return "X" * int(count) + suffix
    return code


def _resolve_value_id(candidates: dict, value_name: str):
    """Match a value name against a {name: taxonomy_value_id} map, allowing
    the short code in parenthesized names like "Small (S)" to match "S", and
    numeral/letter plus-size forms ("3XL"/"XXXL") to match each other."""
    if value_name in candidates:
        return candidates[value_name]
    normalized_value = _normalize_size_code(value_name)
    for name, taxonomy_value_id in candidates.items():
        match = re.search(r"\(([^)]+)\)\s*$", name)
        if not match:
            continue
        code = match.group(1).strip()
        if code.lower() == value_name.strip().lower() or _normalize_size_code(code) == normalized_value:
            return taxonomy_value_id
    return None


def _write_category_metafields(cl, product_id: str, category: dict, values: dict, cache: MetafieldCache) -> dict:
    """
    Resolves each attribute/value pair to a metaobject GID and writes
    everything that resolves in one metafieldsSet call. An attribute that
    can't be resolved (bad value name, no metaobject, no definition) - or one
    Shopify itself rejects (e.g. an "owner subtype" scope mismatch) - is
    reported in the result rather than discarding the rest of the batch.

    Returns {"metafields": [...succeeded...], "userErrors": [...rejected by
    Shopify...], "resolution_errors": [...couldn't even be submitted...]}.
    """
    attrs_by_name = _choice_list_attributes(category)
    name_to_key = cache.attribute_name_to_key()

    metafields_input = []
    resolution_errors = []
    for attr_name, value_names in values.items():
        try:
            if attr_name == "Color":
                # Shopify's admin merges the separate "Pattern" taxonomy
                # attribute into the same "Color" field/metafield (both live
                # in shopify--color-pattern metaobjects).
                candidates = dict(attrs_by_name.get("Color", {}))
                candidates.update(attrs_by_name.get("Pattern", {}))
            else:
                candidates = attrs_by_name.get(attr_name)
            if candidates is None:
                raise ValueError(f"'{attr_name}' is not a valid attribute for this product's category.")

            key = name_to_key.get(attr_name)
            if key is None:
                raise ValueError(
                    f"No metafield definition enabled for '{attr_name}' yet - "
                    f"accept a suggestion for it once in the Shopify admin, then retry."
                )
            metaobject_type = f"shopify--{key}"

            metaobject_ids = []
            for value_name in value_names:
                taxonomy_value_id = _resolve_value_id(candidates, value_name)
                if taxonomy_value_id is None:
                    raise ValueError(f"'{value_name}' is not a valid value for '{attr_name}'.")
                metaobject_id = cache.find_metaobject(metaobject_type, taxonomy_value_id, preferred_label=value_name)
                if metaobject_id is None:
                    raise ValueError(f"No metaobject found for '{attr_name}' = '{value_name}'.")
                metaobject_ids.append(metaobject_id)

            metafields_input.append({
                "ownerId": product_id,
                "namespace": "shopify",
                "key": key,
                "type": "list.metaobject_reference",
                "value": json.dumps(metaobject_ids),
            })
        except ValueError as e:
            resolution_errors.append({"attribute": attr_name, "message": str(e)})

    if not metafields_input:
        return {"metafields": [], "userErrors": [], "resolution_errors": resolution_errors}

    data = cl.graphql(METAFIELDS_SET_MUTATION, {"metafields": metafields_input})
    result = data["metafieldsSet"]
    result["resolution_errors"] = resolution_errors
    return result


def set_category_metafields(cl, product_id: str, values: dict, cache: MetafieldCache = None) -> dict:
    """
    values: {attribute display name (e.g. "Target gender"): [value name, ...]}
    See _write_category_metafields for the result shape. Raises ValueError
    only if the product itself has no category assigned at all.
    """
    product = get_product_category(cl, product_id)
    category = product.get("category")
    if category is None:
        raise ValueError(f"Product {product_id} has no category assigned.")
    cache = cache or MetafieldCache(cl)
    return _write_category_metafields(cl, product_id, category, values, cache)


def set_product_category(cl, product_id: str, category_id: str) -> dict:
    data = cl.graphql(CATEGORY_UPDATE_MUTATION, {"input": {"id": product_id, "category": category_id}})
    result = data["productUpdate"]
    if result["userErrors"]:
        raise RuntimeError(f"productUpdate errors: {json.dumps(result['userErrors'], indent=2)}")
    return result["product"]["category"]


# --- Category (taxonomy) mismatch detection -------------------------------
# Deliberately conservative: only flags/fixes a gross department mismatch
# (e.g. a shoe filed under Sporting Goods > ... > Hiking Pole Accessories).
# Never overrides a category that's already in the right department just
# because its leaf name doesn't literally match productType's last word -
# that could easily be a more specific, still-correct assignment.

VERTICAL_TOP_BRANCH = {
    "clothing": "Apparel & Accessories",
    "shoes": "Apparel & Accessories",
    "jewelry": "Apparel & Accessories",
    "bags": "Apparel & Accessories",
    "accessories": "Apparel & Accessories",
    "underwear": "Apparel & Accessories",
    "swimwear": "Apparel & Accessories",
}


def category_is_gross_mismatch(product_type: str, category: dict) -> bool:
    if not product_type:
        return False
    first_segment = product_type.split("-")[0].strip().lower()
    expected_branch = VERTICAL_TOP_BRANCH.get(first_segment)
    if expected_branch is None:
        return False
    if category is None:
        return True
    return not category["fullName"].startswith(expected_branch)


def find_correct_category(cache: MetafieldCache, product_type: str):
    """
    Best-effort category match from a product's `productType` string (e.g.
    "Shoes - Men - Sneakers" -> last segment "Sneakers"). Returns
    (category_id, category_full_name) on a single unambiguous exact leaf-name
    match, or (None, reason) if it can't confidently decide.
    """
    if not product_type or not product_type.strip():
        return None, "no productType"
    segments = [s.strip() for s in product_type.split("-") if s.strip()]
    if not segments:
        return None, "no productType"
    last = segments[-1]

    results = cache.search_category(last)
    exact_leaf = [c for c in results if c["isLeaf"] and c["name"].lower() == last.lower()]
    if not exact_leaf:
        return None, f"no exact taxonomy match for '{last}'"
    if len(exact_leaf) == 1:
        return exact_leaf[0]["id"], exact_leaf[0]["fullName"]

    is_kids = any(w.lower() in ("kids", "kid", "children", "baby", "babies", "toddler") for w in segments)
    for c in exact_leaf:
        mentions_kids = any(w in c["fullName"] for w in ("Baby", "Children", "Toddler"))
        if mentions_kids == is_kids:
            return c["id"], c["fullName"]

    return None, f"ambiguous taxonomy match for '{last}' ({len(exact_leaf)} candidates)"


def batch_fix(cl, limit: int, apply: bool = False):
    """
    Walk the first `limit` products in the store: fix a grossly mismatched
    category (only when confidently resolvable to a single taxonomy leaf),
    then run the deterministic suggest_category_metafields() rules and
    (if apply) write them. Returns a list of per-product result dicts.
    """
    cache = MetafieldCache(cl)
    results = []
    count = 0
    for node in cl.paginate(PRODUCT_IDS_QUERY, {}, ["products"]):
        if count >= limit:
            break
        count += 1
        product_id = node["id"]
        row = {"id": product_id}
        try:
            product = get_product_category(cl, product_id)
            row["title"] = product["title"]
            row["product_type"] = product.get("productType")
            category = product.get("category")
            row["category_before"] = category["fullName"] if category else None
            row["category_fixed"] = False

            if category_is_gross_mismatch(product.get("productType"), category):
                correct_id, note = find_correct_category(cache, product.get("productType"))
                if correct_id and (category is None or category["id"] != correct_id):
                    if apply:
                        set_product_category(cl, product_id, correct_id)
                        product = get_product_category(cl, product_id)
                        category = product["category"]
                        row["category_fixed"] = True
                    else:
                        row["category_would_fix_to"] = note
                else:
                    row["category_mismatch_unresolved"] = note

            row["category_after"] = category["fullName"] if category else None

            if category is None:
                row["metafields_applied"] = {}
                row["unresolved"] = []
                row["error"] = "no category assigned - could not run metafield suggestions"
                results.append(row)
                continue

            suggestions, unresolved = suggest_category_metafields(product)
            row["unresolved"] = unresolved
            if suggestions and apply:
                write_result = _write_category_metafields(cl, product_id, category, suggestions, cache)
                row["metafields_applied"] = {mf["key"]: mf["value"] for mf in write_result["metafields"]}
                problems = write_result.get("resolution_errors", []) + write_result.get("userErrors", [])
                if problems:
                    row["metafields_failed"] = problems
            else:
                row["metafields_applied"] = suggestions
            row["applied"] = apply
        except Exception as e:
            row["error"] = str(e)
        results.append(row)
    return results


# --- Deterministic keyword-rule suggestions -------------------------------
# Only covers unambiguous, high-confidence cases. Anything not matched here
# is reported as needing a manual/AI read of the product instead of a guess.

GENDER_KEYWORDS = [
    ("herren", "Male"), ("männer", "Male"), ("men's", "Male"), ("mens ", "Male"),
    ("damen", "Female"), ("frauen", "Female"), ("women's", "Female"), ("womens ", "Female"),
    ("unisex", "Unisex"),
]

NECKLINE_KEYWORDS = [
    ("rundhalsausschnitt", ["Round", "Crew"]),
    ("rundhals", ["Round", "Crew"]),
    ("crew neck", ["Crew"]),
    ("v-ausschnitt", ["V-neck"]),
    ("v-neck", ["V-neck"]),
    ("rollkragen", ["Turtle"]),
    ("turtleneck", ["Turtle"]),
    ("stehkragen", ["Mandarin"]),
    ("kapuze", ["Hooded"]),
    ("hooded", ["Hooded"]),
]

SLEEVE_LENGTH_KEYWORDS = [
    ("langarm", "Long"), ("long sleeve", "Long"),
    ("kurzarm", "Short"), ("short sleeve", "Short"),
    ("ärmellos", "Sleeveless"), ("sleeveless", "Sleeveless"),
]

PATTERN_KEYWORDS = [
    ("camouflage", "Camouflage"), ("camo", "Camouflage"), ("tarnmuster", "Camouflage"),
    ("gestreift", "Striped"), ("striped", "Striped"),
    ("kariert", "Checkered"), ("checkered", "Checkered"), ("plaid", "Plaid"),
    ("geblümt", "Floral"), ("floral", "Floral"),
    ("gepunktet", "Dots"), ("polka dot", "Dots"),
]

SIZE_ORDER = ["XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL", "3XL", "4XL"]


def _haystack(product: dict) -> str:
    text = " ".join(filter(None, [
        product.get("title", ""),
        re.sub("<[^>]+>", " ", product.get("descriptionHtml") or ""),
        " ".join(product.get("tags") or []),
    ]))
    return text.lower()


def suggest_category_metafields(product: dict) -> tuple:
    """Returns (suggestions, unresolved_attribute_names)."""
    category = product.get("category")
    if category is None:
        raise ValueError("Product has no category assigned.")

    attr_names = {n["name"] for n in category["attributes"]["nodes"]}
    haystack = _haystack(product)
    suggestions = {}

    if "Target gender" in attr_names:
        for kw, val in GENDER_KEYWORDS:
            if kw in haystack:
                suggestions["Target gender"] = [val]
                break

    if "Neckline" in attr_names:
        for kw, vals in NECKLINE_KEYWORDS:
            if kw in haystack:
                suggestions["Neckline"] = vals
                break

    if "Sleeve length type" in attr_names:
        for kw, val in SLEEVE_LENGTH_KEYWORDS:
            if kw in haystack:
                suggestions["Sleeve length type"] = [val]
                break

    if "Color" in attr_names or "Pattern" in attr_names:
        for kw, val in PATTERN_KEYWORDS:
            if kw in haystack:
                suggestions["Color"] = [val]
                break

    if "Size" in attr_names:
        sizes = set()
        for edge in product.get("variants", {}).get("edges", []):
            for opt in edge["node"].get("selectedOptions", []):
                token = opt["value"].strip().upper()
                if token in SIZE_ORDER:
                    sizes.add(token)
        if sizes:
            suggestions["Size"] = [s for s in SIZE_ORDER if s in sizes]

    unresolved = sorted(attr_names - set(suggestions.keys()))
    return suggestions, unresolved
