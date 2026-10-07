"""The group/category directory cache in routes/common.py.

The frozen build asks for every group and category on most requests (~1,100
requests, ~180 small YAML files each), and re-parsing them with PyYAML's
pure-Python loader was ~95% of the build. get_approved_groups() and
get_categories() now re-read a directory only when its files change, and hand
callers a deep copy so they can mutate the result as they always could.

These pin the contract that makes that safe: a change is always noticed, and a
caller can never corrupt what the next caller sees.
"""
import os

import pytest
import yaml

from calgen import yamlio
from calgen.routes import common


@pytest.fixture
def site(tmp_path, monkeypatch):
    """A scratch site dir with _groups/ and _categories/, as cwd."""
    (tmp_path / '_groups').mkdir()
    (tmp_path / '_categories').mkdir()
    monkeypatch.chdir(tmp_path)
    common._yaml_dir_cache.clear()
    return tmp_path


def _write(path, data, mtime=None):
    path.write_text(yaml.safe_dump(data))
    if mtime is not None:
        os.utime(path, ns=(mtime, mtime))


class TestGroups:
    def test_reads_groups_sorted_with_their_slug_as_id(self, site):
        _write(site / '_groups' / 'b-group.yaml', {'name': 'Beta'})
        _write(site / '_groups' / 'a-group.yaml', {'name': 'Alpha'})

        groups = common.get_approved_groups()

        assert [(g['id'], g['name']) for g in groups] == [
            ('a-group', 'Alpha'), ('b-group', 'Beta')]

    def test_a_rewritten_group_is_noticed(self, site):
        path = site / '_groups' / 'g.yaml'
        _write(path, {'name': 'Old'}, mtime=1_000_000_000)
        assert common.get_approved_groups()[0]['name'] == 'Old'

        # Same size, so only the mtime tells the two apart.
        _write(path, {'name': 'New'}, mtime=2_000_000_000)

        assert common.get_approved_groups()[0]['name'] == 'New'

    def test_an_added_and_a_removed_group_are_noticed(self, site):
        _write(site / '_groups' / 'one.yaml', {'name': 'One'})
        assert [g['id'] for g in common.get_approved_groups()] == ['one']

        _write(site / '_groups' / 'two.yaml', {'name': 'Two'})
        assert [g['id'] for g in common.get_approved_groups()] == ['one', 'two']

        (site / '_groups' / 'one.yaml').unlink()
        assert [g['id'] for g in common.get_approved_groups()] == ['two']

    def test_a_caller_mutating_the_result_cannot_corrupt_the_next_call(self, site):
        _write(site / '_groups' / 'g.yaml',
               {'name': 'Group', 'categories': ['ai', 'cloud']})

        first = common.get_approved_groups()
        first[0]['name'] = 'MUTATED'
        first[0]['categories'].append('crypto')
        first.append({'id': 'bogus'})

        second = common.get_approved_groups()

        assert len(second) == 1
        assert second[0]['name'] == 'Group'
        assert second[0]['categories'] == ['ai', 'cloud']

    def test_a_missing_directory_is_empty_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        common._yaml_dir_cache.clear()
        assert common.get_approved_groups() == []
        assert common.get_categories() == {}

    def test_a_malformed_file_is_skipped_and_the_rest_still_load(self, site, capsys):
        _write(site / '_groups' / 'good.yaml', {'name': 'Good'})
        (site / '_groups' / 'bad.yaml').write_text('name: [unclosed')

        groups = common.get_approved_groups()

        assert [g['id'] for g in groups] == ['good']
        assert 'Error loading group bad.yaml' in capsys.readouterr().out

    def test_different_working_directories_do_not_share_a_cache(self, tmp_path, monkeypatch):
        for name in ('x', 'y'):
            d = tmp_path / name
            (d / '_groups').mkdir(parents=True)
            _write(d / '_groups' / 'g.yaml', {'name': name.upper()})
        common._yaml_dir_cache.clear()

        monkeypatch.chdir(tmp_path / 'x')
        assert common.get_approved_groups()[0]['name'] == 'X'
        monkeypatch.chdir(tmp_path / 'y')
        assert common.get_approved_groups()[0]['name'] == 'Y'


class TestCategories:
    def test_reads_categories_keyed_by_slug(self, site):
        _write(site / '_categories' / 'ai.yaml', {'name': 'AI'})

        assert common.get_categories() == {'ai': {'name': 'AI', 'slug': 'ai'}}

    def test_a_rewritten_category_is_noticed_and_mutation_is_isolated(self, site):
        path = site / '_categories' / 'ai.yaml'
        _write(path, {'name': 'AI'}, mtime=1_000_000_000)
        cats = common.get_categories()
        cats['ai']['name'] = 'MUTATED'
        assert common.get_categories()['ai']['name'] == 'AI'

        _write(path, {'name': 'Ai'}, mtime=2_000_000_000)
        assert common.get_categories()['ai']['name'] == 'Ai'


class TestOnlyReadsWhenFilesChange:
    def test_repeated_calls_parse_each_file_once(self, site, monkeypatch):
        for i in range(5):
            _write(site / '_groups' / f'g{i}.yaml', {'name': f'G{i}'})
        parses = []
        real = yamlio.safe_load
        monkeypatch.setattr(yamlio, 'safe_load',
                            lambda f: parses.append(1) or real(f))

        for _ in range(20):
            common.get_approved_groups()

        assert len(parses) == 5, 'each of the 5 files should be parsed once'


class TestYamlio:
    @pytest.mark.parametrize('text', [
        'a: 1\nb: [x, y]\nc: {d: true}\n',
        'when: 2026-10-07\nversion: 1.10\nflag: no\nempty:\n',
        'title: "Make with Notion — AI, APIs & Developer Platform"\n',
    ])
    def test_loads_exactly_what_pyyaml_safe_load_does(self, text):
        assert repr(yamlio.safe_load(text)) == repr(yaml.safe_load(text))

    @pytest.mark.parametrize('text', ['a: [', 'a: b: c', '\tbad'])
    def test_malformed_yaml_raises_a_yamlerror_like_safe_load(self, text):
        with pytest.raises(yaml.YAMLError):
            yamlio.safe_load(text)
        with pytest.raises(yaml.YAMLError):
            yaml.safe_load(text)

    def test_it_is_the_safe_loader_not_the_full_one(self):
        with pytest.raises(yaml.YAMLError):
            yamlio.safe_load('!!python/object/apply:os.system ["true"]')


def test_the_build_uses_libyaml_when_pyyaml_has_it():
    """PyYAML's PyPI wheels (manylinux, cp312) bundle libyaml. If this fails on a
    machine whose PyYAML reports libyaml, yamlio has stopped picking it up and
    every build is silently ~10x slower."""
    import yaml
    from calgen import yamlio
    if not yaml.__with_libyaml__:
        pytest.skip('this PyYAML has no libyaml')
    assert yamlio.LOADER_NAME == 'CSafeLoader'
