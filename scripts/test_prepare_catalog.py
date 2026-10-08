import contextlib
import gzip
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import prepare_catalog as pipeline
from validate_public_catalog import validate

class PipelineTest(unittest.TestCase):
    aliases = {
        "one.m3u": "CanaisBR01.m3u8", "BR01.m3u": "CanaisBR01.m3u8",
        "two.m3u": "CanaisBR02.m3u8", "BR02.m3u": "CanaisBR02.m3u8",
        "input.m3u": "CanaisBR03.m3u8", "BR03.m3u": "CanaisBR03.m3u8",
        "BR04.m3u": "CanaisBR04.m3u8", "BR05.m3u8": "CanaisBR05.m3u8",
    }
    approved = ["CanaisBR01.m3u8", "CanaisBR02.m3u8", "CanaisBR03.m3u8", "CanaisBR04.m3u8", "Filmes-Series.m3u8"]
    def fixture_path(self, name):
        return self.aliases.get(name, name)
    def ensure_approved_fixtures(self, root, current):
        for name in self.approved:
            target=root/name
            if target != current and not target.exists(): target.write_text('#EXTM3U\n',encoding='utf-8')
    def write(self, root, name, records):
        target=root/self.fixture_path(name)
        target.write_bytes(("#EXTM3U\r\n"+"".join(f'#EXTINF:-1 group-title="Filmes",{title}\r\n{url}\r\n' for title,url in records)).encode("utf-8"))
        self.ensure_approved_fixtures(root,target)
    def write_live(self, root, name, title, tvg_id, url):
        target=root/self.fixture_path(name)
        target.write_text(f'#EXTM3U\n#EXTINF:-1 tvg-id="{tvg_id}" group-title="Esportes",{title}\n{url}\n',encoding="utf-8")
        self.ensure_approved_fixtures(root,target)
    def config(self, *names):
        paths=[self.fixture_path(name) for name in names]
        if not paths or all(path in self.approved for path in paths): paths=self.approved
        return {"upstream":"Ramys/Iptv-Brasil-2026","branch":"master","minimumAppVersion":16,
                "destination":{"repository":"microwan-fordun-as/atlas-index","branch":"main"},
                "inputs":[{"path":path,"section":"auto"} for path in paths]}
    def read(self, output):
        manifest=json.loads((output/"manifest.json").read_text(encoding="utf-8"))
        records=[]
        for partition in manifest["files"]:
            raw=gzip.decompress((output/partition["path"]).read_bytes())
            self.assertEqual(len(raw),partition["uncompressedSize"])
            records.extend(json.loads(line) for line in raw.splitlines())
        return manifest,records

    def test_hash_vectors_exact_utf8_and_no_normalization(self):
        vectors=json.loads((Path(__file__).parent/"stream_key_vectors.json").read_text(encoding="utf-8"))
        for vector in vectors:
            self.assertEqual(vector["streamKey"],pipeline.stream_key(vector["url"]))
        self.assertNotEqual(pipeline.stream_key(vectors[0]["url"]),pipeline.stream_key(vectors[0]["url"].replace('%2f','%2F')))

    def test_credentials_query_and_urls_never_enter_public_output_or_stdout(self):
        with tempfile.TemporaryDirectory(prefix="vinitv-metadata-fixture-") as directory:
            root=Path(directory);output=root/"catalog"
            url="https://synthetic-user:synthetic-password@media.example/movie.mp4?token=synthetic-private-token"
            self.write(root,"input.m3u",[("Synthetic film 2020",url)])
            stdout=io.StringIO()
            with contextlib.redirect_stdout(stdout):
                pipeline.generate(root,self.config("input.m3u"),output,"revision-one")
            manifest,records=self.read(output)
            validate(output)
            self.assertEqual(pipeline.stream_key(url),records[0]["streamKey"])
            self.assertNotIn("url",records[0]);self.assertNotIn("logo",records[0])
            public=json.dumps(manifest)+json.dumps(records)+stdout.getvalue()
            for secret in [url,'synthetic-user','synthetic-password','synthetic-private-token']:
                self.assertNotIn(secret,public)
            self.assertEqual(2,manifest["schemaVersion"])

    def test_unchanged_source_is_not_reparsed_and_partial_change_is_reused(self):
        with tempfile.TemporaryDirectory(prefix="vinitv-incremental-fixture-") as directory:
            root=Path(directory);output=root/"catalog"
            rows=[(f"Synthetic film {i} 2020",f"https://media.example/{i}.mp4?token=synthetic-{i}") for i in range(100)]
            self.write(root,"one.m3u",rows[:50]);self.write(root,"two.m3u",rows[50:])
            config=self.config("one.m3u","two.m3u")
            self.assertTrue(pipeline.generate(root,config,output,"revision-one"))
            before,_=self.read(output)
            with patch.object(pipeline,'records',side_effect=AssertionError("unchanged source was parsed")):
                self.assertFalse(pipeline.generate(root,config,output,"revision-two"))
            altered=list(rows[:50]);altered[0]=(altered[0][0],altered[0][1]+'&changed=1')
            self.write(root,"one.m3u",altered)
            called=[];original=pipeline.records
            def tracked(path,*args):
                called.append(path.name);return original(path,*args)
            with patch.object(pipeline,'records',side_effect=tracked):
                self.assertTrue(pipeline.generate(root,config,output,"revision-three"))
            self.assertEqual(['CanaisBR01.m3u8'],called)
            after,records=self.read(output)
            old_hashes={p['id']:p['sha256'] for p in before['files']}
            self.assertEqual(1,sum(p['sha256']!=old_hashes.get(p['id']) for p in after['files']))
            self.assertEqual(100,len(records))
            print('metadataFixtureRecords=100 inputAndOutputBytes='+str(sum(p.stat().st_size for p in root.rglob('*') if p.is_file())))

    def test_url_change_keeps_work_key(self):
        with tempfile.TemporaryDirectory(prefix="vinitv-work-key-fixture-") as directory:
            root=Path(directory);config=self.config('one.m3u');output=root/'catalog'
            self.write(root,'one.m3u',[('Synthetic film 2020','https://media.example/one.mp4?token=old-secret')])
            pipeline.generate(root,config,output,'one');_,before=self.read(output)
            self.write(root,'one.m3u',[('Synthetic film 2020','https://other.example/two.mp4?token=new-secret')])
            pipeline.generate(root,config,output,'two');_,after=self.read(output)
            self.assertEqual(before[0]['workKey'],after[0]['workKey'])
            self.assertNotEqual(before[0]['streamKey'],after[0]['streamKey'])

    def test_live_channel_identity_survives_rename_domain_and_password_rotation(self):
        with tempfile.TemporaryDirectory(prefix="vinitv-live-identity-fixture-") as directory:
            root=Path(directory);output=root/'catalog';config=self.config('one.m3u')
            self.write_live(root,'one.m3u','News Network HD','news.br','https://old-user:old-password@old.example/live?token=old-token')
            pipeline.generate(root,config,output,'revision-one');_,before=self.read(output)
            self.write_live(root,'one.m3u','News Network Brasil FHD','NEWS.BR','https://new-user:new-password@new.example/live?token=new-token')
            pipeline.generate(root,config,output,'revision-two');_,after=self.read(output)
            self.assertEqual(1,len(after))
            self.assertEqual(before[0]['workKey'],after[0]['workKey'])
            self.assertNotEqual(before[0]['streamKey'],after[0]['streamKey'])
            self.assertEqual('News Network Brasil FHD',after[0]['name'])

    def test_catalog_with_no_records_is_valid_and_published(self):
        with tempfile.TemporaryDirectory(prefix="vinitv-empty-catalog-fixture-") as directory:
            root=Path(directory);target=root/self.fixture_path('one.m3u');target.write_text('#EXTM3U\n',encoding='utf-8');self.ensure_approved_fixtures(root,target)
            output=root/'catalog'
            self.assertTrue(pipeline.generate(root,self.config('one.m3u'),output,'revision-empty'))
            manifest,rows=self.read(output);validate(output)
            self.assertEqual([],manifest['files']);self.assertEqual([],rows)

    def test_validator_blocks_partition_above_github_file_limit(self):
        with tempfile.TemporaryDirectory(prefix='vinitv-github-file-limit-') as directory:
            root=Path(directory);output=root/'catalog'
            self.write(root,'one.m3u',[('Synthetic film 2020','https://media.example/film.mp4')])
            pipeline.generate(root,self.config('one.m3u'),output,'revision-size-limit')
            manifest=json.loads((output/'manifest.json').read_text(encoding='utf-8'))
            manifest['files'][0]['compressedSize']=90*1024*1024+1
            (output/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'GitHub file-size safety limit'):
                validate(output)

    def test_failed_generation_keeps_previous_catalog_byte_for_byte(self):
        with tempfile.TemporaryDirectory(prefix="vinitv-rollback-fixture-") as directory:
            root=Path(directory);output=root/'catalog';config=self.config('one.m3u')
            self.write(root,'one.m3u',[('Synthetic film 2020','https://media.example/film.mp4?token=synthetic-secret')])
            pipeline.generate(root,config,output,'revision-one')
            before={path.relative_to(output).as_posix():path.read_bytes() for path in output.rglob('*') if path.is_file()}
            (root/self.fixture_path('one.m3u')).write_text('#EXTM3U\ninvalid record\n',encoding='utf-8')
            with self.assertRaises(ValueError): pipeline.generate(root,config,output,'revision-two')
            after={path.relative_to(output).as_posix():path.read_bytes() for path in output.rglob('*') if path.is_file()}
            self.assertEqual(before,after);validate(output)

    def test_missing_authorized_playlist_keeps_previous_catalog(self):
        with tempfile.TemporaryDirectory(prefix='vinitv-source-unavailable-fixture-') as directory:
            root=Path(directory);output=root/'catalog';config=self.config('one.m3u')
            self.write(root,'one.m3u',[('Synthetic film 2020','https://media.example/film.mp4?token=synthetic-secret')])
            pipeline.generate(root,config,output,'revision-one')
            before=(output/'manifest.json').read_bytes()
            (root/'CanaisBR04.m3u8').unlink()
            with self.assertRaises(ValueError): pipeline.generate(root,config,output,'revision-two')
            self.assertEqual(before,(output/'manifest.json').read_bytes());validate(output)

    def test_duplicate_streams_merge_source_references_without_duplicate_records(self):
        with tempfile.TemporaryDirectory(prefix='vinitv-duplicate-fixture-') as directory:
            root=Path(directory);url='https://media.example/shared.mp4?token=synthetic-secret'
            self.write(root,'one.m3u',[('Shared film 2020',url),('Different display 2020',url)])
            self.write(root,'two.m3u',[('Shared film 2020',url)])
            output=root/'catalog';pipeline.generate(root,self.config('one.m3u','two.m3u'),output,'revision-duplicates')
            _,rows=self.read(output);validate(output)
            self.assertEqual(1,len(rows));self.assertEqual(2,len(rows[0]['upstreamIds']))

    def test_shared_stream_removed_from_only_one_authorized_source(self):
        with tempfile.TemporaryDirectory(prefix='vinitv-source-removal-fixture-') as directory:
            root=Path(directory);output=root/'catalog';config=self.config('one.m3u','two.m3u')
            rows=[('Synthetic film 2020','https://media.example/shared.mp4?token=synthetic-secret')]
            self.write(root,'one.m3u',rows);self.write(root,'two.m3u',rows)
            pipeline.generate(root,config,output,'one');_,before=self.read(output)
            self.assertEqual(2,len(before[0]['upstreamIds']))
            self.write(root,'one.m3u',[])
            pipeline.generate(root,config,output,'two');_,after=self.read(output)
            self.assertEqual(1,len(after));self.assertEqual(1,len(after[0]['upstreamIds']))
            self.assertEqual(before[0]['workKey'],after[0]['workKey'])

    def test_apple_tv_plus_stays_excluded(self):
        with tempfile.TemporaryDirectory(prefix='vinitv-apple-metadata-fixture-') as directory:
            root=Path(directory);self.write(root,'one.m3u',[('APPLE TV+ HD','https://media.example/apple.m3u8'),('Synthetic film 2020','https://media.example/film.mp4')])
            pipeline.generate(root,self.config('one.m3u'),root/'catalog','one')
            _,records=self.read(root/'catalog');self.assertEqual(1,len(records));self.assertEqual('Synthetic film 2020',records[0]['name'])

    def test_cli_error_does_not_print_url_credentials_or_m3u_lines(self):
        with tempfile.TemporaryDirectory(prefix='vinitv-safe-log-fixture-') as directory:
            root=Path(directory);secret='https://user:synthetic-secret@media.example/file.mp4?private=secret'
            self.write(root,'one.m3u',[(secret,secret)])
            config=root/'config.json';config.write_text(json.dumps(self.config('one.m3u')),encoding='utf-8')
            result=subprocess.run([sys.executable,'-B',str(Path(pipeline.__file__)), '--config',str(config),'--input-root',str(root),'--output',str(root/'catalog')],capture_output=True,text=True)
            self.assertNotEqual(0,result.returncode)
            self.assertNotIn(secret,result.stdout+result.stderr);self.assertNotIn('synthetic-secret',result.stdout+result.stderr)
            self.assertFalse((root/'catalog/manifest.json').exists())

    def test_unexpected_raw_artifact_blocks_publication_audit(self):
        with tempfile.TemporaryDirectory(prefix='vinitv-public-audit-fixture-') as directory:
            root=Path(directory)
            self.write(root,'one.m3u',[('Synthetic film 2020','https://media.example/movie.mp4?token=synthetic-secret')])
            pipeline.generate(root,self.config('one.m3u'),root/'catalog','one')
            (root/'catalog/private.m3u').write_text('synthetic confidential fixture',encoding='utf-8')
            with self.assertRaises(ValueError): validate(root/'catalog')

    def test_unauthorized_paths_are_not_processed(self):
        with tempfile.TemporaryDirectory(prefix='vinitv-authorized-fixture-') as directory:
            root=Path(directory)
            with self.assertRaises(ValueError): pipeline.generate(root,{'inputs':[]},root/'catalog','one')
            with self.assertRaises(ValueError): pipeline.generate(root,self.config('../outside.m3u'),root/'catalog','one')

    def test_release_config_contains_exact_authorized_sources_and_destination(self):
        config=json.loads((Path(__file__).parents[1]/'catalog-config.json').read_text(encoding='utf-8'))
        self.assertEqual(set(self.approved),{item['path'] for item in config['inputs']})
        self.assertTrue(all(item['section']=='auto' for item in config['inputs']))
        self.assertEqual({'repository':'microwan-fordun-as/atlas-index','branch':'main'},config['destination'])
        self.assertEqual(16,config['minimumAppVersion'])
        self.assertNotIn('CanaisBR05.m3u8',{item['path'] for item in config['inputs']})

    def test_review_workflow_is_read_only_off_main_and_publishes_only_changes(self):
        workflow=(Path(__file__).parents[1]/'.github'/'workflows'/'catalog.yml').read_text(encoding='utf-8')
        validation=workflow.split('  validate-branch:',1)[1].split('  publish-main:',1)[0]
        review=workflow.split('  generate-review:',1)[1].split('  publish-main:',1)[0]
        publication=workflow.split('  publish-main:',1)[1]
        self.assertIn('pull_request:',workflow)
        self.assertNotIn('ref: main',validation)
        self.assertIn('contents: read',validation)
        self.assertIn("if: github.ref != 'refs/heads/main'",review)
        self.assertIn('contents: read',review)
        self.assertIn('Prepare metadata-only catalog for review',review)
        self.assertIn('Validate review catalog without publishing',review)
        self.assertNotIn('push origin',review)
        self.assertIn("if: github.ref == 'refs/heads/main'",publication)
        self.assertIn('contents: write',publication)
        self.assertIn('timeout-minutes: 60',publication)
        self.assertIn('git diff --cached --quiet',publication)
        self.assertIn('push origin HEAD:refs/heads/main',publication)

    def test_opt_in_316k_record_catalog_generation(self):
        if os.environ.get('VINITV_BENCHMARK_CATALOG_316K') != '1':
            self.skipTest('Set VINITV_BENCHMARK_CATALOG_316K=1 for one explicit scale run')
        count=316_000
        with tempfile.TemporaryDirectory(prefix='vinitv-catalog-scale-316k-') as directory:
            root=Path(directory);free=shutil.disk_usage(root).free
            required=count*3000+512*1024*1024
            if free<required: self.skipTest('Insufficient free space for the synthetic scale fixture')
            target=root/'CanaisBR01.m3u8'
            with target.open('w',encoding='utf-8',newline='\n') as output:
                output.write('#EXTM3U\n')
                for index in range(count):
                    output.write(f'#EXTINF:-1 group-title="Filmes",Synthetic catalog item {index} 2020\n')
                    output.write(f'https://media.example/scale/{index}.mp4\n')
            self.ensure_approved_fixtures(root,target)
            started=time.monotonic();catalog=root/'catalog'
            self.assertTrue(pipeline.generate(root,self.config('one.m3u'),catalog,'synthetic-316k'))
            manifest=json.loads((catalog/'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(count,sum(item['recordCount'] for item in manifest['files']))
            validate(catalog)
            output_bytes=sum(path.stat().st_size for path in catalog.rglob('*') if path.is_file())
            print(f'catalogScaleRecords={count} inputBytes={target.stat().st_size} outputBytes={output_bytes} elapsedSeconds={time.monotonic()-started:.1f}')


    def test_canonical_deduplication_across_three_sources_preserves_alternatives(self):
        from collections import defaultdict
        with tempfile.TemporaryDirectory(prefix="vinitv-three-source-dedup-") as directory:
            root=Path(directory);output=root/"catalog"
            def url(key):
                return "https://fixture-user:fixture-password@media.example/"+key+"?token=fixture-private-token"
            sources={
                "BR01.m3u": [("Shared film 2020 HD",url("movie-a")),("Undated film HD",url("undated-a")),("Shared show S01E01 HD",url("episode1-a")),("Shared show S01E02",url("episode2-shared"))],
                "BR02.m3u": [("Shared film 2020 FHD",url("movie-b")),("Shared film 2021",url("remake")),("Undated film FHD",url("undated-b")),("Shared show S01E01 FHD",url("episode1-b")),("Shared show S01E02",url("episode2-shared"))],
                "BR03.m3u": [("Shared film 2020 HD",url("movie-a")),("Undated film 4K",url("undated-c")),("Shared show S01E01 4K",url("episode1-c")),("Shared show S01E03",url("episode3-exclusive"))]
            }
            for name,rows in sources.items(): self.write(root,name,rows)
            config=self.config(*sources)
            for entry in config["inputs"]: entry["section"]="auto"
            self.assertTrue(pipeline.generate(root,config,output,"three-sources"))
            manifest,rows=self.read(output);validate(output)
            works=defaultdict(list)
            for row in rows: works[row["workKey"]].append(row)
            movies={key:items for key,items in works.items() if items[0]["kind"]=="movie"}
            episodes={key:items for key,items in works.items() if items[0]["kind"]=="episode"}
            parents={row["parentWorkKey"] for row in rows if row["kind"]=="episode"}
            self.assertEqual(13,sum(map(len,sources.values())))
            self.assertEqual(11,len(rows));self.assertEqual(11,len({r["streamKey"] for r in rows}))
            self.assertEqual(3,len(movies));self.assertEqual(3,len(episodes));self.assertEqual(1,len(parents))
            self.assertEqual([1,2,3],sorted({r["episode"] for r in rows if r["kind"]=="episode"}))
            self.assertEqual([1,2,3],sorted(len(items) for items in movies.values()))
            self.assertEqual([1,1,3],sorted(len(items) for items in episodes.values()))
            shared_movie=[r for r in rows if r["streamKey"]==pipeline.stream_key(url("movie-a"))][0]
            shared_episode=[r for r in rows if r["streamKey"]==pipeline.stream_key(url("episode2-shared"))][0]
            self.assertEqual(2,len(shared_movie["upstreamIds"]));self.assertEqual(2,len(shared_episode["upstreamIds"]))
            self.assertEqual(13,sum(len(r["upstreamIds"]) for r in rows))
            remake=[r for r in rows if r["year"]==2021][0]
            self.assertNotEqual(shared_movie["workKey"],remake["workKey"])
            self.assertEqual(5,sum(len(items)-1 for items in works.values()))
            # Card identities are movie workKeys plus series parentWorkKeys, not streamKeys.
            self.assertEqual(4,len(set(movies)|parents))
            public=json.dumps(manifest)+json.dumps(rows)
            for secret in ["fixture-user","fixture-password","fixture-private-token"]:
                self.assertNotIn(secret,public)
            print("threeSourceFixture rawEntries=13 canonicalWorksIncludingSeries=7 movies=3 series=1 episodes=3 uniqueStreams=11 extraAlternatives=5 sourceAssociations=13 cardIdentities=4")
            # Removing BR01 must keep BR03's identical movie and BR02's identical episode.
            self.write(root,"BR01.m3u",[])
            self.assertTrue(pipeline.generate(root,config,output,"source-one-removed"))
            _,remaining=self.read(output);validate(output)
            self.assertEqual(set(works),{r["workKey"] for r in remaining})
            self.assertEqual(9,len(remaining))
            for key in [shared_movie["streamKey"],shared_episode["streamKey"]]:
                record=next(r for r in remaining if r["streamKey"]==key)
                self.assertEqual(1,len(record["upstreamIds"]))

if __name__ == '__main__':
    unittest.main()
