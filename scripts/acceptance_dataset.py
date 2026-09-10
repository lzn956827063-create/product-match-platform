"""Generate deterministic acceptance data and an independently constructed truth set."""
import argparse
import csv
import hashlib
import io
import json
from collections import Counter
from pathlib import Path

from scripts.seed import HEADERS

GENERATOR_VERSION = "acceptance-dataset-v1"
BRANDS = ("华为", "小米", "OPPO", "vivo", "荣耀", "Samsung", "Apple", "中兴")
COLORS = ("黑色", "白色", "蓝色", "银色")
REGIONS = ("国行", "港版")
CAPACITIES = ("128", "256", "512", "1024")


def sha256(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def model_for(index):
    number = f"{index:06d}"
    variants = (f"M-{number}", f"M/{number}", f"M{number}", f"M{number}A")
    return variants[index % len(variants)]


def product(org_number, index, seed=0):
    variant = index + seed
    brand = BRANDS[variant % len(BRANDS)]
    model = model_for(variant)
    storage = CAPACITIES[variant % len(CAPACITIES)]
    color = COLORS[variant % len(COLORS)]
    region = REGIONS[variant % len(REGIONS)]
    sku = f"STD-{org_number:02d}-{index:06d}"
    name = f"{brand} {model} 8+{storage} {color} {region} 旗舰手机"
    return [sku, name, brand, model, "8", storage, color, region, "1", str(1800 + variant % 5000), "CNY"]


def source_and_truth(org_number, index, catalog_size, seed=0):
    target = product(org_number, index % catalog_size, seed)
    row = target.copy()
    row[0] = f"SRC-{org_number:02d}-{index:06d}"
    bucket = index % 20
    if bucket < 14:
        category = "unique_or_normalized"
        reason = "唯一型号与完整规格指向固定标准商品"
    elif bucket < 17:
        category = "composite"
        reason = "品牌、型号、名称和规格组合指向固定标准商品"
        row[1] = row[1].replace("旗舰手机", "原封 单台").replace(" ", "　" if bucket == 14 else " ")
        row[3] = row[3].lower() if bucket == 15 else row[3].replace("-", " - ").replace("/", " / ")
    elif bucket < 19:
        category = "insufficient"
        reason = "品牌和型号缺失，预期进入人工补资料"
        row[1] = f"待补资料 手机 {index:06d}"
        row[2] = ""
        row[3] = ""
    else:
        category = "conflict"
        reason = "容量与目标标准商品冲突，预期进入人工冲突处理"
        row[5] = CAPACITIES[(CAPACITIES.index(row[5]) + 1) % len(CAPACITIES)]
        row[1] = f"{row[2]} {row[3]} 8+{row[5]} {row[6]} {row[7]} 旗舰手机"
    truth = {
        "org_key": f"org-{org_number}",
        "source_sku": row[0],
        "category": category,
        "expected_standard_sku": target[0] if category != "insufficient" else None,
        "expected_outcome": "candidate" if category in {"unique_or_normalized", "composite"} else "needs_info" if category == "insufficient" else "conflict",
        "reason": reason,
    }
    return row, truth


def write_csv(path, rows):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(HEADERS)
    writer.writerows(rows)
    path.write_bytes(b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8"))


def generate(destination, catalog_size, source_size, organizations, users, seed):
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError("Dataset destination already exists; use a fresh acceptance run directory")
    if catalog_size < 20 or source_size < 20 or organizations < 2 or users < 2:
        raise ValueError("Use at least 20 catalog rows, 20 sources, 2 organizations and 2 users")
    destination.mkdir(parents=True)
    files = {}
    aggregate = Counter()
    for org_number in range(1, organizations + 1):
        folder = destination / f"org-{org_number}"
        folder.mkdir()
        catalog_path = folder / "catalog.csv"
        source_path = folder / "source.csv"
        truth_path = folder / "truth.jsonl"
        write_csv(catalog_path, (product(org_number, index, seed) for index in range(catalog_size)))
        source_rows = []
        truths = []
        for index in range(source_size):
            row, truth = source_and_truth(org_number, index, catalog_size, seed)
            source_rows.append(row)
            truths.append(truth)
            aggregate[truth["category"]] += 1
        write_csv(source_path, source_rows)
        truth_path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in truths))
        for path in (catalog_path, source_path, truth_path):
            files[str(path.relative_to(destination))] = {"bytes": path.stat().st_size, "sha256": sha256(path)}
    total = organizations * source_size
    manifest = {
        "generator_version": GENERATOR_VERSION,
        "generator_sha256": sha256(Path(__file__)),
        "seed": seed,
        "organizations": organizations,
        "users_per_organization": users,
        "historical_releases_per_organization": 10,
        "catalog_rows_per_organization": catalog_size,
        "source_rows_per_organization": source_size,
        "truth_distribution": {
            key: {"rows": aggregate[key], "ratio": aggregate[key] / total}
            for key in ("unique_or_normalized", "composite", "insufficient", "conflict")
        },
        "scope": "Deterministic synthetic acceptance data; not customer data or real-business accuracy evidence",
        "files": files,
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return manifest


def verify(source):
    source = Path(source).resolve()
    manifest = json.loads((source / "manifest.json").read_text())
    for name, expected in manifest["files"].items():
        path = (source / name).resolve()
        if not path.is_relative_to(source) or not path.is_file():
            raise ValueError("Dataset manifest references an invalid path: " + name)
        if path.stat().st_size != expected["bytes"] or sha256(path) != expected["sha256"]:
            raise ValueError("Dataset hash mismatch: " + name)
    if manifest["generator_version"] != GENERATOR_VERSION:
        raise ValueError("Unsupported acceptance dataset generator version")
    return manifest


def load(source):
    source = Path(source).resolve()
    manifest = verify(source)
    from sqlalchemy import func, select
    from packages.domain.auth import Context, password_hash
    from packages.domain.db import transaction, uid
    from packages.domain.models import Batch, Catalog, DatasetVersion, Membership, Run, User
    from packages.domain.services import create_catalog_version, create_revision, create_run
    from packages.matching.normalize import digest
    from scripts.seed import MAPPING, add_file, main as seed_demo
    from workers.jobs import process_run

    seed_demo()
    definitions = []
    for org_number, domain in enumerate(("demo", "other"), 1):
        if org_number > manifest["organizations"]:
            break
        with transaction(write=True) as session:
            actor = session.scalar(select(User).where(User.email == f"operator@{domain}.local"))
            membership = session.scalar(select(Membership).where(Membership.user_id == actor.id))
            context = Context(membership.org_id, actor.id, ["operator", "admin"])
            dataset_name = f"数据库验收固定数据 seed={manifest['seed']}"
            exists = session.scalar(select(DatasetVersion.id).where(DatasetVersion.org_id == context.org_id, DatasetVersion.name == dataset_name))
            if exists:
                raise ValueError("This fixed dataset is already loaded; rebuild the isolated acceptance environment")
            base = session.scalar(select(Run).where(Run.org_id == context.org_id).order_by(Run.created_at))
            if not base:
                raise ValueError("Synthetic demo policy was not initialized")
            catalog_content = (source / f"org-{org_number}" / "catalog.csv").read_bytes()
            source_content = (source / f"org-{org_number}" / "source.csv").read_bytes()
            catalog = Catalog(id=uid(), org_id=context.org_id, name="数据库验收固定标准库")
            session.add(catalog)
            session.flush()
            catalog_file = add_file(session, context.org_id, f"acceptance-{org_number}-catalog.csv", catalog_content)
            version = create_catalog_version(session, context, catalog, {"file_id": catalog_file.id, "sheet": "CSV", "header_row": 1, "mapping": MAPPING})
            version.status = "PUBLISHED"
            batch = Batch(id=uid(), org_id=context.org_id, name="数据库验收固定来源", supplier=f"构造供应商 {org_number}", created_by=actor.id)
            session.add(batch)
            session.flush()
            source_file = add_file(session, context.org_id, f"acceptance-{org_number}-source.csv", source_content)
            revision = create_revision(session, context, batch, {"file_id": source_file.id, "sheet": "CSV", "header_row": 1, "mapping": MAPPING, "exclude_rows": []})
            run = create_run(session, context, {"revision_id": revision.id, "catalog_version_id": version.id, "policy_id": base.policy_id})
            session.add(DatasetVersion(
                org_id=context.org_id,
                name=dataset_name,
                provenance={"kind": "constructed", "generator": GENERATOR_VERSION, "seed": manifest["seed"]},
                status="FROZEN",
                manifest={
                    "catalog_rows": manifest["catalog_rows_per_organization"],
                    "source_rows": manifest["source_rows_per_organization"],
                    "truth_sha256": manifest["files"][f"org-{org_number}/truth.jsonl"]["sha256"],
                },
                content_hash=digest(manifest["files"]),
            ))
            existing_users = session.scalar(
                select(func.count()).select_from(Membership).where(Membership.org_id == context.org_id)
            )
            for index in range(existing_users, manifest["users_per_organization"]):
                email = f"acceptance-{domain}-{index}@test.local"
                if session.scalar(select(User.id).where(User.email == email)):
                    continue
                user = User(id=uid(), email=email, name=f"验收用户 {org_number}-{index + 1}", password_hash=password_hash("Acceptance2026!match"))
                session.add(user)
                session.flush()
                session.add(Membership(org_id=context.org_id, user_id=user.id, roles=["operator", "reviewer", "publisher"]))
            definitions.append({
                "org_id": context.org_id,
                "domain": domain,
                "catalog_version_id": version.id,
                "batch_id": batch.id,
                "revision_id": revision.id,
                "run_id": run["id"],
                "policy_id": base.policy_id,
                "users": manifest["users_per_organization"],
            })
        process_run(run["id"])
    history = create_release_history(definitions, manifest["historical_releases_per_organization"])
    result = {
        "dataset_manifest_sha256": sha256(source / "manifest.json"),
        "definitions": definitions,
        "historical_releases": history,
    }
    (source / "loaded.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def create_release_history(definitions, count):
    from sqlalchemy import select
    from packages.domain.auth import Context
    from packages.domain.db import transaction
    from packages.domain.models import BatchRelease, Candidate, Item, Membership, Revision, Run, User
    from packages.domain.releases import create_release, publish_release, queue_validation
    from packages.domain.services import decide
    from workers.releases import process_files, process_validation

    results = []
    for definition in definitions:
        with transaction(write=True) as session:
            members = session.execute(
                select(Membership, User)
                .join(User, User.id == Membership.user_id)
                .where(Membership.org_id == definition["org_id"], Membership.active.is_(True), User.active.is_(True))
            ).all()
            membership, actor = next(
                (member, user)
                for member, user in members
                if {"reviewer", "publisher"}.issubset(member.roles)
            )
            context = Context(definition["org_id"], actor.id, membership.roles)
            baseline = session.scalar(
                select(Run)
                .where(Run.org_id == definition["org_id"], Run.id != definition["run_id"], Run.status == "SUCCEEDED")
                .order_by(Run.created_at)
            )
            if not baseline:
                raise ValueError("Synthetic seed run is required before creating acceptance release history")
            revision = session.get(Revision, baseline.revision_id)
            items = session.scalars(
                select(Item).where(Item.org_id == definition["org_id"], Item.run_id == baseline.id).order_by(Item.id)
            ).all()
            for item in items:
                candidate = session.scalar(
                    select(Candidate)
                    .where(Candidate.org_id == definition["org_id"], Candidate.item_id == item.id)
                    .order_by(Candidate.rank)
                )
                if item.suggestion == "RECOMMENDED" and candidate:
                    action = {"action": "confirm", "product_id": candidate.product_id, "expected_version": item.version}
                else:
                    action = {"action": "unmatched", "reason": "构造历史发布：非高置信项保留为未匹配", "expected_version": item.version}
                decide(session, context, item.id, action)
            baseline_id = baseline.id
            batch_id = revision.batch_id

        release_ids = []
        base_release_id = None
        for _ in range(count):
            with transaction(write=True) as session:
                config = {"baseline_run_id": baseline_id}
                if base_release_id:
                    config["base_release_id"] = base_release_id
                release = create_release(session, context, batch_id, config)
                queue_validation(session, context, release)
                release_id = release.id
            process_validation(release_id)
            with transaction(write=True) as session:
                release = session.get(BatchRelease, release_id)
                if release.status != "READY":
                    raise AssertionError("Constructed release history failed validation")
                publish_release(session, context, release, release.version)
            process_files(release_id)
            release_ids.append(release_id)
            base_release_id = release_id
        results.append({
            "org_id": definition["org_id"],
            "releases": len(release_ids),
            "release_ids": release_ids,
            "current_release_id": base_release_id,
        })
    return results


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("generate")
    create.add_argument("destination")
    create.add_argument("--catalog-size", type=int, default=50000)
    create.add_argument("--source-size", type=int, default=10000)
    create.add_argument("--organizations", type=int, default=2)
    create.add_argument("--users", type=int, default=10)
    create.add_argument("--seed", type=int, default=1309)
    check = commands.add_parser("verify")
    check.add_argument("source")
    loader = commands.add_parser("load")
    loader.add_argument("source")
    args = parser.parse_args()
    if args.command == "generate":
        result = generate(args.destination, args.catalog_size, args.source_size, args.organizations, args.users, args.seed)
    elif args.command == "verify":
        result = verify(args.source)
    else:
        result = load(args.source)
    print(json.dumps({key: result[key] for key in result if key != "files"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
