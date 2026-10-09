import unittest
from unittest.mock import patch, MagicMock
import server

class ReleaseNotesTests(unittest.TestCase):
    def setUp(self):server.release_notes.cache_clear()
    def tearDown(self):server.release_notes.cache_clear()

    def test_changelog_preserves_order_and_multiline_contents(self):
        entries=server.changelog_entries('# 更新紀錄\n\n## 新版\n\n- 第一項\n- 第二項\n\n## 舊版\n\n- 歷史內容')
        self.assertEqual([e['title'] for e in entries],['新版','舊版'])
        self.assertIn('- 第二項',entries[0]['content'])

    def test_release_notes_follow_pinned_platform_config_and_revision(self):
        digest='sha256:'+'a'*64;platform='sha256:'+'b'*64;config='sha256:'+'c'*64;revision='d'*40
        response=MagicMock();response.__enter__.return_value=response;response.read.return_value=b'## Current release\n\n- Fixed OTA\n\n## Previous release\n\n- History'
        metadata=[{'token':'public-token'},{'manifests':[{'digest':platform,'platform':{'os':'linux','architecture':'amd64'}}]},{'config':{'digest':config}},{'config':{'Labels':{'org.opencontainers.image.source':'https://github.com/cp296944/islevetah','org.opencontainers.image.revision':revision}}}]
        with patch.object(server,'release_json',side_effect=metadata) as lookup,patch.object(server,'urlopen',return_value=response) as download:
            result=server.release_notes(digest)
        self.assertTrue(lookup.call_args_list[1].args[0].endswith('/manifests/'+digest))
        self.assertTrue(lookup.call_args_list[2].args[0].endswith('/manifests/'+platform))
        self.assertIn('/'+revision+'/CHANGELOG.md',download.call_args.args[0].full_url)
        self.assertEqual(result['revision'],revision)
        self.assertEqual(len(result['entries']),2)

    def test_invalid_digest_rejected_without_network(self):
        with patch.object(server,'release_json') as lookup,self.assertRaises(server.APIError) as error:
            server.release_notes('latest')
        self.assertEqual(error.exception.status,400);lookup.assert_not_called()

    def test_registry_failure_can_be_retried(self):
        with patch.object(server,'release_json',side_effect=server.URLError('offline')) as lookup:
            for _ in range(2):
                with self.assertRaises(server.APIError) as error:server.release_notes('sha256:'+'a'*64)
                self.assertEqual(error.exception.status,503)
        self.assertEqual(lookup.call_count,2)
