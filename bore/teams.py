"""League team names the text tools know (sports stories): a team's nickname is never a person's
name, and neither is its city in front of it. Data only: cards take their colours from the page's
theme, never from a team."""
from __future__ import annotations

NFL_TEAMS = ("cardinals", "falcons", "ravens", "bills", "panthers", "bears", "bengals", "browns", "cowboys", "broncos",
             "lions", "packers", "texans", "colts", "jaguars", "chiefs", "raiders", "chargers", "rams", "dolphins",
             "vikings", "patriots", "saints", "giants", "jets", "eagles", "steelers", "49ers", "seahawks", "buccaneers",
             "titans", "commanders", "nfl")
# The teams' cities: where a team plays ("Green Bay Packers"), never whom a story is about
NFL_CITIES = ("Arizona", "Atlanta", "Baltimore", "Buffalo", "Carolina", "Chicago", "Cincinnati", "Cleveland", "Dallas",
              "Denver", "Detroit", "Green Bay", "Houston", "Indianapolis", "Jacksonville", "Kansas City", "Las Vegas",
              "Los Angeles", "Miami", "Minnesota", "New England", "New Orleans", "New York", "Philadelphia",
              "Pittsburgh", "San Francisco", "Seattle", "Tampa Bay", "Tennessee", "Washington")

__all__ = ["NFL_TEAMS", "NFL_CITIES"]
