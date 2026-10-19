import pytest

from session_bridge.catalog import same_directory, search_matches


@pytest.mark.parametrize(
    "recorded,requested,expected",
    [
        ("C:\\Work\\App\\", "c:/work/app/./", True),
        ("C:/Work/App", "C:/Work/Application", False),
        ("C:/Work/App/nested", "C:/Work/App", False),
        ("\\\\?\\C:\\Work\\App", "c:/work/app", True),
        ("\\\\?\\UNC\\host\\share\\app", "\\\\host\\share\\app", True),
        ("/work/App/", "/work/App/./", True),
        ("/work/App", "/work/app", False),
        ("/work/app-other", "/work/app", False),
        ("/work/app/nested", "/work/app", False),
        ("", "/work/app", False),
        ("app", "/work/app", False),
    ],
)
def test_directory_scope_matches_whole_paths_without_changing_foreign_path_semantics(
    recorded, requested, expected
):
    assert same_directory(recorded, requested) is expected


@pytest.mark.parametrize(
    "query,expected",
    [
        ("DISENA busqueda", True),
        ("búsqueda   motor", True),
        ("motor — de", True),
        ("orquest", False),
        ("", True),
        ("???", False),
    ],
)
def test_metadata_search_handles_partial_words_accents_and_punctuation(query, expected):
    assert search_matches(query, "Diseña un motor de búsqueda") is expected
