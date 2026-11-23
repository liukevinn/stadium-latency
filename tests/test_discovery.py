from courtside.discovery import GAME_SERIES_RE, Game, GameMarket, search


def game(ticker: str, title: str, *teams: str) -> Game:
    return Game(
        ticker,
        "KXNCAAFGAME",
        title,
        tuple(GameMarket(f"{ticker}-{t[:3].upper()}", t, "", "", "") for t in teams),
    )


GAMES = [
    game("KXNCAAFGAME-26NOV20DUKECLEM", "Duke vs Clemson", "Duke", "Clemson"),
    game("KXNCAAFGAME-26NOV21UNCNCST", "North Carolina vs NC State", "North Carolina", "NC State"),
]


def test_search_matches_all_words_case_insensitive() -> None:
    assert [g.title for g in search(GAMES, "duke")] == ["Duke vs Clemson"]
    assert [g.title for g in search(GAMES, "CLEMSON duke")] == ["Duke vs Clemson"]
    assert search(GAMES, "duke state") == []
    assert len(search(GAMES, "")) == 2


def test_game_series_regex() -> None:
    assert GAME_SERIES_RE.match("KXNBAGAME")
    assert GAME_SERIES_RE.match("KXNCAAFGAME")
    assert not GAME_SERIES_RE.match("KXNBAGAME7")
    assert not GAME_SERIES_RE.match("KXPPLGAMES")
