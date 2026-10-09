"""Metadata-only public catalog. Playback URL bytes never enter generated artifacts or logs."""
import argparse
import gzip
import hashlib
import json
import os
import re
import subprocess
import shutil
import sqlite3
import sys
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit, parse_qsl, unquote

SECTIONS = {"tv", "movies", "series", "novelas", "unknown", "auto"}
APPROVED_REPOSITORY = "Ramys/Iptv-Brasil-2026"
APPROVED_BRANCH = "master"
APPROVED_INPUTS = frozenset({"CanaisBR01.m3u8", "CanaisBR02.m3u8", "CanaisBR03.m3u8", "CanaisBR04.m3u8", "Filmes-Series.m3u8"})
ATTR = re.compile(r'''([\w-]+)\s*=\s*["']([^"'\r\n]*)["']''')
EPISODE = re.compile(r"(?i)\b(?:s|t)(\d{1,3})\s*e(\d{1,4})\b|\b(\d{1,2})x(\d{2,3})\b|\btemporada\s*(\d{1,3})\D{0,12}?epis[oó]dio\s*(\d{1,4})\b")
EXCLUDED = re.compile(r"(?<![a-z0-9])apple[\s._-]*tv(?:[\s._-]*\+|[\s._-]*plus)(?![a-z0-9])", re.I)
YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")
QUALITY = re.compile(r"(?i)\b(?:4k|uhd|2160p|fhd|full\s*hd|1080p|hd|720p|sd|480p|hdr10?|dolby\s*vision|x26[45]|h\.?26[45]|hevc|web[- ]?dl|blu[- ]?ray)\b")
LANGUAGE = re.compile(r"(?i)\b(?:dual\s*[áa]udio|dual[- ]audio|dublado|dubbed|legendado|sub(?:bed)?|pt[- ]?br|portugu[eê]s|english|ingl[eê]s|espanhol|spanish)\b")
TRAILING_YEAR = re.compile(r"(?:\s*[\(\[]\s*(19\d{2}|20\d{2})\s*[\)\]]|\s+(19\d{2}|20\d{2}))\s*$")


def stream_key(original):
    return hashlib.sha256(original.encode("utf-8")).hexdigest()


def preparation_hash(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode("utf-8") + Path(__file__).read_bytes()).hexdigest()


def normalize(value):
    value = TRAILING_YEAR.sub(" ", LANGUAGE.sub(" ", QUALITY.sub(" ", EPISODE.sub(" ", value))).strip(' -._|:'))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", "".join(c for c in unicodedata.normalize("NFD", value.lower()) if not unicodedata.combining(c))).split())


def display(value):
    return " ".join(TRAILING_YEAR.sub(" ", LANGUAGE.sub(" ", QUALITY.sub(" ", value)).strip(' -._|:')).split()).strip(' -._|:')


def valid_stream(original):
    try:
        uri = urlsplit(original)
        return (uri.scheme.lower() in {"http", "https"} and bool(uri.hostname)
                and len(original) <= 4096 and not any(c.isspace() for c in original))
    except ValueError:
        return False


def public_text(value, original):
    """Fail closed if copied metadata contains a URL or an actual embedded secret."""
    uri = urlsplit(original)
    secrets = [unquote(v) for v in (uri.username, uri.password) if v]
    secrets += [v for _, v in parse_qsl(uri.query, keep_blank_values=True) if len(v) >= 4]
    if "://" in value or re.search(r"(?i)%3a%2f%2f",value) or original in value or any(len(secret) >= 4 and secret in value for secret in secrets):
        raise ValueError("Sensitive content in upstream metadata")
    return value


def records(path, declared_section, source_id):
    if declared_section not in SECTIONS:
        raise ValueError("Unsupported section")
    metadata = None
    header = False
    with path.open(encoding="utf-8-sig", errors="strict", newline="") as source:
        for raw in source:
            if len(raw) > 32768:
                raise ValueError("Input record too long")
            # Only remove record delimiters. Never trim/normalize/decode a playback URL.
            line = raw.removesuffix("\n").removesuffix("\r")
            if not line:
                continue
            if not header:
                if not line.upper().startswith("#EXTM3U"):
                    raise ValueError("Invalid upstream header")
                header = True
                continue
            if line.startswith("#EXTINF:"):
                attrs = {key.lower(): value for key,value in ATTR.findall(line)}
                quoted = None
                title = ""
                for index, char in enumerate(line):
                    if char in {'"', "'"}:
                        quoted = None if quoted == char else char if quoted is None else quoted
                    elif char == ',' and quoted is None:
                        title = line[index+1:].strip()
                        break
                metadata = (title or attrs.get("tvg-name", ""), attrs)
            elif not line.startswith("#"):
                if not valid_stream(line):
                    raise ValueError("Invalid upstream stream")
                if metadata is None:
                    raise ValueError("Missing upstream record metadata")
                title, attrs = metadata
                metadata = None
                category = attrs.get("group-title", "Importados").strip() or "Importados"
                if any(EXCLUDED.search(value) for value in (title, category, attrs.get("tvg-name", ""), attrs.get("tvg-id", ""))):
                    continue
                if not title or len(title) > 200 or len(category) > 120:
                    raise ValueError("Invalid upstream metadata")
                for value in (title, category, attrs.get("tvg-name", ""), attrs.get("tvg-id", "")):
                    public_text(value, line)
                parsed = EPISODE.search(title)
                season = int(parsed[1] or parsed[3] or parsed[5]) if parsed else 0
                number = int(parsed[2] or parsed[4] or parsed[6]) if parsed else 0
                series = title[:parsed.start()].strip(' -') if parsed else ""
                section = declared_section
                if section == "auto":
                    category_key = normalize(category)
                    if parsed:
                        section = "novelas" if "novela" in category_key else "series"
                    elif any(word in category_key for word in ("filme", "movie", "vod")) or re.search(r"(?i)/(?:movie|movies|vod)/", urlsplit(line).path):
                        section = "movies"
                    else:
                        section = "tv"
                if section in {"series", "novelas"} and not parsed:
                    section = "unknown"
                year_match = TRAILING_YEAR.search(LANGUAGE.sub(" ",QUALITY.sub(" ",EPISODE.sub(" ",title))).strip(' -._|:'))
                year = int(next(group for group in year_match.groups() if group)) if year_match else 0
                canonical_title = display(title) or title
                normalized = normalize(title)
                kind = {"movies": "movie", "tv": "live", "series": "episode", "novelas": "episode", "unknown": "unknown"}[section]
                parent_key = ""
                if kind == "episode":
                    parent_key = stream_key(f"series|{section}|{normalize(series)}|{year}")
                    work_key = stream_key(f"episode|{parent_key}|{season}|{number}")
                elif kind == "live":
                    tvg_id = attrs.get("tvg-id", "").strip().casefold()
                    work_key = stream_key(f"live|tvg|{tvg_id}") if tvg_id else stream_key(f"live|name|{normalized}")
                else:
                    # Known movies share identity across files even when year is absent.
                    # Unknown content remains source-scoped to avoid speculative merging.
                    scope = source_id if kind == "unknown" and year == 0 else ""
                    work_key = stream_key(f"{kind}|{normalized}|{year}|{scope}")
                quality = "4k" if re.search(r"(?i)\b(?:4k|uhd|2160p)\b",title) else "fhd" if re.search(r"(?i)\b(?:fhd|1080p)\b",title) else "hd" if re.search(r"(?i)\b(?:hd|720p)\b",title) else ""
                audio = attrs.get("tvg-language", attrs.get("audio-language", ""))[:80]
                subtitles = attrs.get("subtitle-language", "")[:80]
                if not audio and re.search(r"(?i)\b(?:dublado|dubbed|pt[- ]?br|portugu[eê]s)\b", title): audio = "pt-BR"
                if not audio and re.search(r"(?i)\bdual[- ]audio\b", title): audio = "multiple"
                if not audio and re.search(r"(?i)\b(?:english|ingl[eê]s)\b", title): audio = "en"
                if not subtitles and re.search(r"(?i)\b(?:legendado|leg|subbed)\b", title): subtitles = "pt-BR"
                public_text(audio, line); public_text(subtitles, line)
                yield {"streamKey": stream_key(line), "upstreamIds": [source_id], "name": title,
                       "category": category, "section": section, "series": series, "season": season,
                       "episode": number, "year": year, "durationSeconds": -1, "hints": 0,
                       "tvgName": attrs.get("tvg-name", "")[:200], "tvgId": attrs.get("tvg-id", "")[:200],
                       "audioLanguage": audio, "subtitleLanguage": subtitles, "quality": quality,
                       "workKey": work_key, "kind": kind, "canonicalTitle": canonical_title,
                       "normalizedTitle": normalized, "parentWorkKey": parent_key,
                       "seriesTitle": display(series), "seriesNormalizedTitle": normalize(series),
                       "categoryKey": normalize(category), "search": normalize(title + ' ' + category + ' ' + series)}
    if not header:
        raise ValueError("Missing upstream header")


def file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def generate(input_root, config, output, revision):
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", revision):
        raise ValueError("Invalid public revision")
    if not config.get("inputs") or len(config["inputs"]) > 32:
        raise ValueError("Configure authorized upstream files before publication")
    repo, branch = config.get("upstream", ""), config.get("branch", "")
    if repo != APPROVED_REPOSITORY or branch != APPROVED_BRANCH:
        raise ValueError("Upstream repository is outside the approved catalog scope")
    if config.get("destination") != {"repository": "microwan-fordun-as/atlas-index", "branch": "main"}:
        raise ValueError("Catalog destination is outside the approved repository")
    if not isinstance(config.get("minimumAppVersion"), int) or config["minimumAppVersion"] < 16:
        raise ValueError("Unsupported minimum app version")
    old_path = output / "manifest.json"
    old = json.loads(old_path.read_text(encoding="utf-8")) if old_path.exists() else {}
    config_hash = preparation_hash(config)
    descriptors, targets = [], {}
    seen_paths = set()
    for entry in config["inputs"]:
        path = entry["path"]
        relative = Path(path)
        target = (input_root / relative).resolve()
        if (path not in APPROVED_INPUTS or entry.get("section") != "auto" or path in seen_paths
                or relative.is_absolute() or '..' in relative.parts
                or not target.is_relative_to(input_root.resolve()) or not target.is_file()):
            raise ValueError("Input is outside the approved catalog scope")
        seen_paths.add(path)
        source_id = "upstream-" + stream_key(f"{repo}|{branch}|{path}")[:48]
        if source_id in targets:
            raise ValueError("Duplicate upstream input")
        descriptor = {"id": source_id, "url": f"https://raw.githubusercontent.com/{repo}/{quote(revision, safe='')}/{quote(path, safe='/')}",
                      "sha256": file_digest(target), "size": target.stat().st_size}
        if descriptor["size"] < 1 or descriptor["size"] > 1024**3:
            raise ValueError("Upstream size outside contract limits")
        descriptors.append(descriptor)
        targets[source_id] = (target, entry["section"])
    if seen_paths != APPROVED_INPUTS:
        raise ValueError("All five approved upstream files must be configured")
    old_hashes = {item["id"]: item["sha256"] for item in old.get("upstreamFiles", [])}
    unchanged = {item["id"] for item in descriptors if old.get("schemaVersion") == 2 and old.get("preparationHash") == config_hash and old_hashes.get(item["id"]) == item["sha256"]}
    files = []
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vinitv-catalog-index-") as work_directory:
        index_db = sqlite3.connect(Path(work_directory) / "index.sqlite3")
        try:
            index_db.execute("PRAGMA journal_mode=OFF")
            index_db.execute("PRAGMA synchronous=OFF")
            index_db.execute("CREATE TABLE records(stream_key TEXT PRIMARY KEY, work_key TEXT NOT NULL, payload TEXT NOT NULL)")
            index_db.execute("CREATE INDEX records_bucket_key ON records(substr(work_key,1,1),stream_key)")
            index_db.execute("CREATE TABLE record_sources(stream_key TEXT NOT NULL, source_id TEXT NOT NULL, PRIMARY KEY(stream_key,source_id))")
            index_db.execute("CREATE INDEX record_sources_key ON record_sources(stream_key,source_id)")

            def add_record(record):
                public = dict(record)
                refs = public.pop("upstreamIds")
                public_json = json.dumps(public, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                index_db.execute("INSERT OR IGNORE INTO records(stream_key,work_key,payload) VALUES(?,?,?)",
                                 (record["streamKey"], record["workKey"], public_json))
                index_db.executemany("INSERT OR IGNORE INTO record_sources(stream_key,source_id) VALUES(?,?)",
                                     ((record["streamKey"], ref) for ref in refs))

            # Reuse only already-public metadata for unchanged inputs, never raw cached M3Us.
            if unchanged:
                for partition in old.get("files", []):
                    with gzip.open(output/partition["path"], "rt", encoding="utf-8") as previous:
                        for line in previous:
                            record = json.loads(line)
                            refs = sorted(set(record["upstreamIds"]) & unchanged)
                            if refs:
                                record["upstreamIds"] = refs
                                add_record(record)
            for source_id, (target, section) in targets.items():
                if source_id not in unchanged:
                    for record in records(target, section, source_id):
                        add_record(record)
                    index_db.commit()

            candidate = Path(tempfile.mkdtemp(prefix=f".{output.name}-candidate-", dir=output.parent))
            backup = candidate.with_name(candidate.name + "-previous")
            try:
                partition_dir = candidate / "partitions"
                partition_dir.mkdir()
                for index in range(16):
                    source_id = f"remote-shard-{index:02x}"
                    temporary = partition_dir / f".{source_id}.jsonl.gz.tmp"
                    raw_size = 0
                    record_count = 0
                    with temporary.open("wb") as compressed_file:
                        with gzip.GzipFile(fileobj=compressed_file, mode="wb", compresslevel=6, mtime=0) as compressed:
                            current_key = None
                            current_record = None
                            current_refs = []
                            cursor = index_db.execute(
                                "SELECT r.stream_key,r.payload,s.source_id FROM records r "
                                "JOIN record_sources s ON s.stream_key=r.stream_key "
                                "WHERE substr(r.work_key,1,1)=? ORDER BY r.stream_key,s.source_id", (f"{index:x}",))
                            for key, payload, ref in cursor:
                                if current_key is not None and key != current_key:
                                    current_record["upstreamIds"] = current_refs
                                    line = (json.dumps(current_record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
                                    compressed.write(line); raw_size += len(line); record_count += 1
                                    current_record = None; current_refs = []
                                if key != current_key:
                                    current_key = key
                                    current_record = json.loads(payload)
                                current_refs.append(ref)
                            if current_key is not None:
                                current_record["upstreamIds"] = current_refs
                                line = (json.dumps(current_record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
                                compressed.write(line); raw_size += len(line); record_count += 1
                    if not record_count:
                        temporary.unlink()
                        continue
                    digest = file_digest(temporary)
                    compressed_size = temporary.stat().st_size
                    path = f"partitions/{source_id}-{digest}.jsonl.gz"
                    temporary.replace(candidate / path)
                    files.append({"id":source_id,"sourceId":source_id,"displayName":f"Catálogo {index+1}","path":path,
                                  "type":"metadata-jsonl","sha256":digest,"compressedSize":compressed_size,
                                  "uncompressedSize":raw_size,"compression":"gzip","recordCount":record_count})

                semantic_upstream = lambda items: [{"id":i["id"],"sha256":i["sha256"],"size":i["size"]} for i in items]
                unchanged_output = (old.get("schemaVersion") == 2 and files == old.get("files")
                                    and semantic_upstream(descriptors) == semantic_upstream(old.get("upstreamFiles", []))
                                    and config.get("minimumAppVersion",15) == old.get("minimumAppVersion")
                                    and config_hash == old.get("preparationHash"))
                if unchanged_output:
                    from validate_public_catalog import validate
                    validate(output)
                    return False

                manifest = {"manifestVersion":1,"schemaVersion":2,"catalogVersion":old.get("catalogVersion",0)+1,
                            "generatedAt":datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),"sourceRevision":revision,
                            "preparationHash":config_hash,"minimumAppVersion":max(16,config.get("minimumAppVersion",16)),
                            "upstreamFiles":descriptors,"files":files}
                if old.get("schemaVersion") == 2:
                    (candidate/"previous-manifest.json").write_text(json.dumps(old,ensure_ascii=False),encoding="utf-8")
                    for previous in old.get("files", []):
                        previous_partition = (output / previous["path"]).resolve()
                        if not previous_partition.is_relative_to(output.resolve()):
                            raise ValueError("Unsafe previous partition path")
                        retained_partition = candidate / previous["path"]
                        if not retained_partition.exists():
                            retained_partition.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(previous_partition, retained_partition)
                (candidate/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,separators=(",", ":")),encoding="utf-8")
                from validate_public_catalog import validate
                validate(candidate)
                if output.exists():
                    if not output.is_dir():
                        raise ValueError("Catalog output is not a directory")
                    output.replace(backup)
                try:
                    candidate.replace(output)
                except Exception:
                    if backup.exists() and not output.exists():
                        backup.replace(output)
                    raise
                shutil.rmtree(backup, ignore_errors=True)
                return True
            finally:
                shutil.rmtree(candidate, ignore_errors=True)
                if backup.exists() and output.exists():
                    shutil.rmtree(backup, ignore_errors=True)
        finally:
            index_db.close()


def git_output(args):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    return subprocess.run(["git","-c","credential.helper="]+args,check=True,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env).stdout


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--config",default="catalog-config.json")
    parser.add_argument("--output",default="catalog")
    parser.add_argument("--input-root")
    parser.add_argument("--revision",default="local-fixture")
    args=parser.parse_args()
    config=json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.input_root:
        print("changed" if generate(Path(args.input_root),config,Path(args.output),args.revision) else "unchanged")
        return
    if not config.get("inputs"):
        raise ValueError("Configure authorized upstream files")
    repo,branch=config["upstream"],config["branch"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",repo) or not re.fullmatch(r"[A-Za-z0-9_./-]+",branch) or branch.startswith('-'):
        raise ValueError("Invalid public upstream configuration")
    revision=git_output(["ls-remote",f"https://github.com/{repo}.git","refs/heads/"+branch]).split()[0]
    existing=Path(args.output)/"manifest.json"
    if existing.exists():
        old=json.loads(existing.read_text(encoding="utf-8"))
        if old.get("schemaVersion") == 2 and old.get("sourceRevision") == revision and old.get("preparationHash") == preparation_hash(config):
            print("unchanged upstream")
            return
    with tempfile.TemporaryDirectory(prefix="vinitv-public-upstream-") as directory:
        checkout=Path(directory)/"upstream"
        git_output(["clone","--quiet","--depth","1","--single-branch","--branch",branch,f"https://github.com/{repo}.git",str(checkout)])
        revision=git_output(["-C",str(checkout),"rev-parse","HEAD"]).strip()
        print("changed" if generate(checkout,config,Path(args.output),revision) else "unchanged")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never print exceptions, raw records, URLs, config contents or tracebacks.
        print("Catalog preparation failed. Previous published catalog remains unchanged.",file=sys.stderr)
        sys.exit(1)
