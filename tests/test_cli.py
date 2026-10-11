from gamesavesync.main import main


def test_unknown_profile(app_home, capsys):
    code = main(["pull", "Missing"])
    assert code == 1
    assert "Missing" in capsys.readouterr().err


def test_sync_all_with_no_profiles(app_home, capsys):
    assert main(["sync-all"]) == 0
    assert capsys.readouterr().out == ""
