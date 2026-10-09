import gzip
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

FIELDS = set("streamKey upstreamIds name category section series season episode year durationSeconds hints tvgName tvgId audioLanguage subtitleLanguage quality workKey kind canonicalTitle normalizedTitle parentWorkKey seriesTitle seriesNormalizedTitle categoryKey search".split())
APPROVED_REPOSITORY = "Ramys/Iptv-Brasil-2026"
APPROVED_BRANCH = "master"
APPROVED_INPUTS = {"CanaisBR01.m3u8", "CanaisBR02.m3u8", "CanaisBR03.m3u8", "CanaisBR04.m3u8", "Filmes-Series.m3u8"}
MAX_GITHUB_PARTITION_BYTES = 90 * 1024 * 1024

def validate(output):
    manifests = [output/"manifest.json"]
    if (output/"previous-manifest.json").exists(): manifests.append(output/"previous-manifest.json")
    allowed = {path.resolve() for path in manifests}
    files = {}
    for path in manifests:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("schemaVersion") != 2 or manifest.get("manifestVersion") != 1: raise ValueError("Public metadata schema required")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,200}",manifest.get("sourceRevision","")): raise ValueError("Unsafe revision")
        if int(manifest.get("minimumAppVersion", 0)) < 16: raise ValueError("Unsupported minimum app version")
        source_ids = set()
        source_names = set()
        for source in manifest["upstreamFiles"]:
            uri = urlsplit(source["url"])
            path = unquote(uri.path)
            parts = path.strip("/").split("/")
            if (uri.scheme != "https" or uri.hostname != "raw.githubusercontent.com" or uri.port or uri.username or uri.password
                    or uri.query or uri.fragment or len(parts) != 4
                    or parts != APPROVED_REPOSITORY.split("/") + [manifest["sourceRevision"], parts[-1] if parts else ""]
                    or parts[-1] not in APPROVED_INPUTS or source.get("id") in source_ids
                    or not re.fullmatch(r"[a-f0-9]{64}", source.get("sha256", ""))
                    or not isinstance(source.get("size"), int) or source["size"] <= 0):
                raise ValueError("Unsafe public upstream endpoint")
            source_ids.add(source["id"])
            source_names.add(parts[-1])
        if source_names != APPROVED_INPUTS or len(source_ids) != len(APPROVED_INPUTS):
            raise ValueError("Public catalog source scope mismatch")
        for item in manifest["files"]:
            size = item.get("compressedSize")
            if not isinstance(size, int) or not 0 < size <= MAX_GITHUB_PARTITION_BYTES:
                raise ValueError("Partition exceeds GitHub file-size safety limit")
            if not re.fullmatch(r"partitions/remote-shard-[0-9a-f]{2}-[0-9a-f]{64}\.jsonl\.gz",item["path"]): raise ValueError("Unsafe partition path")
            target=(output/item["path"]).resolve()
            if not target.is_relative_to(output.resolve()): raise ValueError("Partition escapes catalog")
            allowed.add(target)
            files[target]=item
    for path in output.rglob("*"):
        if path.is_file() and (path.is_symlink() or path.resolve() not in allowed): raise ValueError("Unexpected public catalog artifact")
    for path,item in files.items():
        digest=hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda:source.read(65536),b""): digest.update(chunk)
        if digest.hexdigest()!=item["sha256"] or path.stat().st_size!=item["compressedSize"]: raise ValueError("Partition integrity mismatch")
        count=0
        with gzip.open(path,"rt",encoding="utf-8") as source:
            for line in source:
                record=json.loads(line)
                if set(record)-FIELDS or not re.fullmatch("[a-f0-9]{64}",record.get("streamKey","")): raise ValueError("Unsafe public metadata record")
                if not set(record.get("upstreamIds", [])) <= source_ids: raise ValueError("Unknown public upstream reference")
                if any(isinstance(value,str) and ("://" in value or re.search(r"(?i)%3a%2f%2f", value)) for value in record.values()): raise ValueError("URL-like public metadata")
                count+=1
        if count != item["recordCount"]: raise ValueError("Record count mismatch")

if __name__ == "__main__":
    try:
        validate(Path("catalog"))
        print("Public metadata validation passed")
    except Exception:
        raise SystemExit("Public metadata validation failed")
