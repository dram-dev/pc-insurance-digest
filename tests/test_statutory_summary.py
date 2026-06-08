"""Free statutory summary — III top-writer table parsing → statutory_facts.

Network-free: a synthetic III-style fragment (only row 1 carries $/%; the rest
are bare, as the live page renders) drives the parser and the injected-fetch run.
"""
from __future__ import annotations

from digest import db
from digest.ingest import statutory_summary as ss

_HTML = ("<table><tr><td>1</td><td>State Farm</td><td>$67,748,192</td><td>18.9%</td></tr>"
         "<tr><td>2</td><td>Progressive</td><td>60,053,469</td><td>16.7</td></tr>"
         "<tr><td>3</td><td>Berkshire Hathaway Inc.</td><td>41,714,394</td><td>11.6</td></tr></table>")


def test_parse_top_writers():
    rows = ss.parse_top_writers(_HTML)
    assert [r[0] for r in rows] == [1, 2, 3]                  # contiguous rank block
    assert rows[0][1] == "State Farm" and rows[0][2] == 67_748_192 and rows[0][3] == 18.9
    assert rows[1][1] == "Progressive" and rows[1][2] == 60_053_469


def test_run_writes_statutory_facts(fresh_db):
    res = ss.run_statutory_summary(_fetch=lambda url: _HTML)
    assert "state_farm" in res["insurer_list"]
    with db.get_conn() as c:
        sf = c.execute("""SELECT value FROM statutory_facts WHERE insurer='state_farm'
                          AND dataset='premiums' LIMIT 1""").fetchone()
        share = c.execute("""SELECT value FROM statutory_facts WHERE insurer='state_farm'
                             AND dataset='market_share' LIMIT 1""").fetchone()
    assert sf["value"] == 67748.192          # $000 → $M
    assert share["value"] == 18.9
