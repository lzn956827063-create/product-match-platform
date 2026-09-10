"""Versioned smartphone SKU normalization. Unknown values remain unknown."""
import hashlib
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation

RULE_VERSION = "phone-cn-1.0.0"
REQUIRED = ["brand", "model", "ram", "storage", "color", "region", "pack_count"]
LABELS = {"brand": "品牌", "model": "型号", "ram": "运行内存", "storage": "存储容量", "color": "颜色", "region": "销售版本", "pack_count": "包装数量"}
BRANDS = {"apple": "apple", "苹果": "apple", "iphone": "apple", "samsung": "samsung", "三星": "samsung", "xiaomi": "xiaomi", "小米": "xiaomi", "redmi": "redmi", "红米": "redmi", "华为": "huawei", "huawei": "huawei", "荣耀": "honor", "honor": "honor", "oppo": "oppo", "vivo": "vivo", "a牌": "a牌", "b牌": "b牌"}
COLORS = {"黑色": "黑色", "曜石黑": "黑色", "black": "黑色", "白色": "白色", "white": "白色", "蓝色": "蓝色", "blue": "蓝色", "银色": "银色", "silver": "银色", "紫色": "紫色", "purple": "紫色", "绿色": "绿色", "green": "绿色", "金色": "金色", "gold": "金色"}
REGIONS = {"国行": "国行", "中国大陆": "国行", "港版": "港版", "香港": "港版", "美版": "美版", "us": "美版", "国际版": "国际版", "global": "国际版"}


def digest(value):
    raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def clean(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip().lower()


def capacity(value):
    value = clean(value).replace(" ", "")
    if not value:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(gib|tib|gb|tb|g|t)?", value)
    if not m:
        return None
    n, unit = Decimal(m[1]), m[2] or "gb"
    if unit in ("tb", "t", "tib"):
        n *= 1024 if unit == "tib" else 1000
    return f"{n.normalize():f}{'GiB' if unit in ('gib', 'tib') else 'GB'}"


def normalize(fields):
    name = str(fields.get("name") or "").strip()
    text = clean(name + " " + str(fields.get("specs") or ""))
    result = {"name": name, "search_text": clean(name), "category": "phone", "rule_version": RULE_VERSION, "provenance": {}, "issues": []}

    def put(key, value, origin):
        result[key] = value or None
        result["provenance"][key] = origin if value else "missing"

    brand = clean(fields.get("brand"))
    inferred_brand = next((v for k, v in BRANDS.items() if k in text), None)
    put("brand", BRANDS.get(brand, brand) if brand else inferred_brand, "column" if brand else "name")
    model = clean(fields.get("model"))
    # Keep digits, hyphens and suffixes; never strip region/model suffixes.
    m = re.search(r"(?:iphone\s*\d{1,2}(?:\s*(?:pro\s*max|pro|plus|mini))?|galaxy\s*[asz]\d{1,3}(?:\s*(?:ultra|plus|fe))?|(?:redmi|小米)\s*\d{1,2}(?:\s*pro)?|(?:mate|pura|nova)\s*\d{1,3}(?:\s*pro)?|\b[a-z]\d{1,6}(?:-[a-z0-9]+)*)(?![a-z0-9-])", text)
    inferred_model = m[0] if m else None
    put("model", model or (m[0] if m else None), "column" if model else "name")
    pair = re.search(r"(?<!\d)(\d{1,2})\s*(?:gb|g)?\s*\+\s*(\d{2,4})\s*(?:gb|g)?(?!\d)", text)
    for key, idx in (("ram", 1), ("storage", 2)):
        raw = fields.get(key)
        val = capacity(raw)
        if raw and not val:
            result["issues"].append(f"{LABELS[key]}无法解析：{raw}")
        put(key, val if raw else (capacity(pair[idx]) if pair else None), "column" if raw else "name")
    inferred_fields = {}
    for key, aliases in (("color", COLORS), ("region", REGIONS)):
        raw = clean(fields.get(key))
        inferred = [v for k, v in aliases.items() if k in text]
        inferred_fields[key] = inferred[0] if len(set(inferred)) == 1 else None
        put(key, aliases.get(raw, raw) if raw else (inferred[0] if len(set(inferred)) == 1 else None), "column" if raw else "name")
    pack_match = re.search(r"(\d+)\s*(?:台装|件装|台/套|件/套)", text)
    inferred_pack = pack_match[1] if pack_match else ("1" if any(x in text for x in ("单机", "单件", "单台")) else "")
    pack = clean(fields.get("pack_count"))
    if not pack:
        pack = inferred_pack
    put("pack_count", int(pack) if pack.isdigit() and 0 < int(pack) < 10000 else None, "column" if fields.get("pack_count") else "name")
    # Explicit columns which contradict reliably parsed name values need correction.
    for key, inferred in (("brand", inferred_brand), ("ram", capacity(pair[1]) if pair else None), ("storage", capacity(pair[2]) if pair else None), ("color", inferred_fields["color"]), ("region", inferred_fields["region"]), ("pack_count", int(inferred_pack) if inferred_pack else None)):
        if fields.get(key) and inferred and result[key] != inferred:
            result["issues"].append(f"{LABELS[key]}字段与名称冲突")
    if model and inferred_model and model != inferred_model and not model.endswith(" " + inferred_model):
        result["issues"].append("型号字段与名称冲突")
    try:
        p = Decimal(str(fields.get("price"))) if fields.get("price") not in (None, "") else None
        result["price"] = str(p) if p is not None and p.is_finite() and p >= 0 else None
        if p is not None and result["price"] is None:
            result["issues"].append("价格格式无效")
    except InvalidOperation:
        result["price"] = None
        result["issues"].append("价格格式无效")
    result["currency"] = clean(fields.get("currency")).upper() or None
    result["missing"] = [k for k in REQUIRED if result[k] is None]
    return result


def compare(a, b):
    evidence, conflicts, missing = [], [], []
    for key in REQUIRED:
        av, bv = a.get(key), b.get(key)
        if av is None or bv is None:
            missing.append(key)
            evidence.append({"field": key, "label": LABELS[key], "state": "missing", "source": av, "target": bv, "text": f"{LABELS[key]}信息不足"})
        elif av != bv:
            conflicts.append(key)
            evidence.append({"field": key, "label": LABELS[key], "state": "conflict", "source": av, "target": bv, "text": f"{LABELS[key]}冲突"})
        else:
            evidence.append({"field": key, "label": LABELS[key], "state": "same", "source": av, "target": bv, "text": f"{LABELS[key]}一致"})
    identity_issues_a = [x for x in a.get("issues", []) if not x.startswith("价格")]
    identity_issues_b = [x for x in b.get("issues", []) if not x.startswith("价格")]
    if identity_issues_a or identity_issues_b:
        conflicts.append("data_quality")
        evidence.append({"field": "data_quality", "label": "数据质量", "state": "conflict", "source": identity_issues_a, "target": identity_issues_b, "text": "字段解析或来源信息存在问题"})
    return evidence, conflicts, missing
