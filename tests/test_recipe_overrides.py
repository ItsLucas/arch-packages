import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
AUR_COMMIT = '8dfae2ecfcd85ca9d0cdd23d588e2af86a7b18fd'

class RecipeOverrideTests(unittest.TestCase):
    def module(self):
        spec = importlib.util.spec_from_file_location('recipe_overrides', ROOT / 'scripts/recipe_overrides.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_only_reviewed_i7z_recipe_is_patched_and_source_is_commit_pinned(self):
        mod = self.module()
        source = (ROOT / 'overrides/i7z.original.PKGBUILD').read_bytes()
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            path = Path(tmp) / 'PKGBUILD'
            path.write_bytes(source)
            self.assertTrue(mod.apply('i7z', AUR_COMMIT, path))
            text = path.read_text()
            self.assertIn('#commit=ae4191d5102cbee43fce9a85f86f9a9dcd4dab07', text)
            self.assertIn('c13b9e300a8ddd9fa5a2ebc93745816d218d95cffc2dfd66777d458b865c7d94a3d10c892a89deb6c73b01008dee117dc6131caddf9250c65945fbecf1f0aec1', text)
            self.assertNotIn('SKIP', text)
            path.write_bytes(source + b'\n# unexpected edit\n')
            with self.assertRaises(ValueError):
                mod.apply('i7z', AUR_COMMIT, path)
            path.write_bytes(source)
            self.assertFalse(mod.apply('i7z', '0' * 40, path))
            self.assertEqual(path.read_bytes(), source)
            self.assertFalse(mod.apply('google-chrome', AUR_COMMIT, path))

if __name__ == '__main__':
    unittest.main()
