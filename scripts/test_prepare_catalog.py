import gzip
import json
import tempfile
import unittest
from pathlib import Path
from prepare_catalog import generate, public_url

class PipelineTest(unittest.TestCase):
    def test_incremental_deterministic_and_public_only(self):
        with tempfile.TemporaryDirectory(prefix="vinitv-stage5-fixture-") as directory:
            root = Path(directory)
            source = root / "input.m3u"
            urls = [f"https://media.example/movie-{i}.mp4" for i in range(100)]
            def write(title="Film"):
                source.write_text("#EXTM3U\n" + "".join(f'#EXTINF:-1 group-title="Filmes",{title if i==0 else "Film"} {i}\n{url}\n' for i,url in enumerate(urls)), encoding="utf-8")
            config = {"inputs": [{"path": "input.m3u", "section": "movies"}], "publicStreamUrls": urls, "minimumAppVersion": 12}
            output = root / "catalog"
            write()
            self.assertTrue(generate(root, config, output, "revision-one"))
            before = json.loads((output/"manifest.json").read_text())
            self.assertEqual(100, sum(item["recordCount"] for item in before["files"]))
            self.assertFalse(generate(root, config, output, "revision-other"))
            write("Changed")
            self.assertTrue(generate(root, config, output, "revision-two"))
            after = json.loads((output/"manifest.json").read_text())
            changed = [item for item,previous in zip(after["files"], before["files"]) if item["sha256"] != previous["sha256"]]
            self.assertEqual(1, len(changed))
            for item in after["files"]:
                raw = gzip.decompress((output/item["path"]).read_bytes())
                self.assertEqual(item["uncompressedSize"], len(raw))
            print("fixtureRecords=100 retainedCatalogBytes=" + str(sum(p.stat().st_size for p in output.rglob("*") if p.is_file()))
                  + " peakInputAndOutputBytes=" + str(sum(p.stat().st_size for p in root.rglob("*") if p.is_file())))

    def test_secrets_and_empty_publication_are_rejected(self):
        for url in ["https://user:pass@example.test/file.mp4", "https://example.test/file.mp4?token=secret", "file:///etc/passwd"]:
            self.assertFalse(public_url(url))
        with tempfile.TemporaryDirectory(prefix="vinitv-stage5-fixture-") as directory:
            with self.assertRaises(ValueError):
                generate(Path(directory), {}, Path(directory)/"catalog", "fixture")

if __name__ == "__main__":
    unittest.main()
