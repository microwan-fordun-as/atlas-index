"""Public, credential-free catalog builder. Standard library only; never probes streams.
Requires explicit input paths/sections and an exact allowlist of legitimately public URLs.
"""
import argparse
import gzip
import hashlib
import json
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

SECTIONS = {"tv", "movies", "series", "novelas", "unknown"}
ATTR = re.compile(r'([\w-]+)="([^"\r\n]*)"')
EPISODE = re.compile(r'(?i)\b(?:s|t)(\d{1,3})\s*e(\d{1,4})\b|\b(\d{1,2})x(\d{2,3})\b')
YEAR = re.compile(r'\b(19\d{2}|20\d{2})\b')

def public_url(value):
    try:
        uri = urlsplit(value)
        return (uri.scheme in {"https", "http"} and bool(uri.hostname)
                and uri.username is None and uri.password is None
                and not uri.query and not uri.fragment and not any(c.isspace() for c in value))
    except ValueError:
        return False

def records(path, section, allowed):
    if section not in SECTIONS:
        raise ValueError("Unsupported section")
    metadata = None
    with path.open(encoding="utf-8-sig", errors="strict") as source:
        for line in source:
            if len(line) > 32768:
                raise ValueError("Input record too long")
            line = line.strip()
            if line.startswith("#EXTINF:"):
                attrs = dict(ATTR.findall(line))
                # The delimiter is the first comma outside quoted attribute values.
                quoted = False
                title = ""
                for index, char in enumerate(line):
                    if char == '"':
                        quoted = not quoted
                    elif char == ',' and not quoted:
                        title = line[index+1:].strip()
                        break
                metadata = (title, attrs)
            elif line and not line.startswith("#"):
                if metadata is None:
                    continue
                title, attrs = metadata
                metadata = None
                if line not in allowed or not public_url(line):
                    continue
                category = attrs.get("group-title", "Importados").strip() or "Importados"
                if not title or len(title) > 200 or len(category) > 120:
                    raise ValueError("Invalid public record metadata")
                episode = EPISODE.search(title)
                season = int(episode[1] or episode[3]) if episode else 0
                number = int(episode[2] or episode[4]) if episode else 0
                series = title[:episode.start()].strip(' -') if episode else ""
                effective = section
                if section in {"series", "novelas"} and not episode:
                    effective = "unknown"
                year = YEAR.search(title)
                # Artwork is intentionally not transported in Stage 5.
                yield {"name": title, "url": line, "logo": "", "category": category,
                       "section": effective, "series": series, "season": season, "episode": number,
                       "year": int(year[1]) if year else 0, "durationSeconds": -1, "hints": 0,
                       "tvgName": attrs.get("tvg-name", "")[:200], "tvgId": attrs.get("tvg-id", "")[:200],
                       "audioLanguage": "", "subtitleLanguage": ""}

def generate(input_root, config, output, revision):
    allowed = set(config.get("publicStreamUrls", []))
    if not config.get("inputs") or not allowed or not all(public_url(url) for url in allowed):
        raise ValueError("Configure authorized input paths and publicStreamUrls before publication")
    buckets = [dict() for _ in range(16)]
    for entry in config["inputs"]:
        relative = Path(entry["path"])
        target = (input_root / relative).resolve()
        if relative.is_absolute() or not target.is_relative_to(input_root.resolve()) or not target.is_file():
            raise ValueError("Input path is outside the upstream checkout")
        for record in records(target, entry["section"], allowed):
            key = hashlib.sha256(record["url"].encode()).hexdigest()
            buckets[int(key[0], 16)].setdefault(record["url"], record)
    old_path = output / "manifest.json"
    old = json.loads(old_path.read_text(encoding="utf-8")) if old_path.exists() else {}
    config_hash = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    files = []
    payloads = {}
    for index, bucket in enumerate(buckets):
        if not bucket:
            continue
        raw = b"".join((json.dumps(bucket[url], ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
                       for url in sorted(bucket))
        zipped = gzip.compress(raw, compresslevel=6, mtime=0)
        digest = hashlib.sha256(zipped).hexdigest()
        source_id = f"remote-shard-{index:02x}"
        path = f"partitions/{source_id}-{digest}.jsonl.gz"
        payloads[path] = zipped
        files.append({"id": source_id, "sourceId": source_id, "displayName": f"Catálogo público {index+1}",
                      "path": path, "type": "source-jsonl", "sha256": digest,
                      "compressedSize": len(zipped), "uncompressedSize": len(raw),
                      "compression": "gzip", "recordCount": len(bucket)})
    if not files:
        raise ValueError("No authorized records; refusing to replace a valid catalog with an empty catalog")
    if files == old.get("files") and config.get("minimumAppVersion", 13) == old.get("minimumAppVersion"):
        return False
    output.mkdir(parents=True, exist_ok=True)
    for path, payload in payloads.items():
        target = output / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(payload)
    manifest = {"manifestVersion": 1, "schemaVersion": 1, "catalogVersion": old.get("catalogVersion", 0)+1,
                "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "sourceRevision": revision, "preparationHash": config_hash, "minimumAppVersion": config.get("minimumAppVersion", 13), "files": files}
    # Keep exactly the previous manifest and its files; clients do not retain downloaded copies.
    if old:
        (output / "previous-manifest.json").write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    temporary = output / "manifest.json.tmp"
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(old_path)
    retained = {item["path"] for item in files + old.get("files", [])}
    for candidate in (output / "partitions").glob("remote-shard-*.jsonl.gz"):
        if candidate.relative_to(output).as_posix() not in retained:
            candidate.unlink()
    return True

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="catalog-config.json")
    parser.add_argument("--output", default="catalog")
    parser.add_argument("--input-root")
    parser.add_argument("--revision", default="local-fixture")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.input_root:
        print("changed" if generate(Path(args.input_root), config, Path(args.output), args.revision) else "unchanged")
        return
    repo, branch = config["upstream"], config["branch"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not re.fullmatch(r"[A-Za-z0-9_./-]+", branch) or branch.startswith("-"):
        raise ValueError("Invalid public upstream configuration")
    # Authentication is neither configured nor required for this public clone.
    revision = subprocess.check_output(["git", "-c", "credential.helper=", "ls-remote", f"https://github.com/{repo}.git", "refs/heads/" + branch], text=True).split()[0]
    existing = Path(args.output) / "manifest.json"
    config_hash = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    if existing.exists():
        old = json.loads(existing.read_text(encoding="utf-8"))
        if old.get("sourceRevision") == revision and old.get("preparationHash") == config_hash:
            print("unchanged upstream")
            return
    with tempfile.TemporaryDirectory(prefix="vinitv-public-upstream-") as directory:
        checkout = Path(directory) / "upstream"
        subprocess.run(["git", "-c", "credential.helper=", "clone", "--quiet", "--depth", "1", "--single-branch", "--branch", branch,
                        f"https://github.com/{repo}.git", str(checkout)], check=True)
        revision = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
        print("changed" if generate(checkout, config, Path(args.output), revision) else "unchanged")

if __name__ == "__main__":
    main()
