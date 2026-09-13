import unittest
from provision_agents import omp_asset


class OmpAssetTests(unittest.TestCase):
    def asset(self):
        return {'name': 'omp-linux-x64', 'size': 100,
                'browser_download_url': 'https://github.com/can1357/oh-my-pi/releases/download/v1/omp-linux-x64'}

    def test_selects_target_architecture(self):
        asset = self.asset()
        self.assertEqual(omp_asset({'assets': [{'name': 'omp-linux-arm64'}, asset]}), asset)

    def test_missing_duplicate_and_unexpected_source_fail(self):
        asset = self.asset()
        for assets in ([], [asset, asset], [{**asset, 'browser_download_url': 'http://example.com/file'}]):
            with self.subTest(assets=assets), self.assertRaises(RuntimeError):
                omp_asset({'assets': assets})


if __name__ == '__main__':
    unittest.main()
