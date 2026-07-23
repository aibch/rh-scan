"""UI-contract tests for the report's two top-level dashboard tabs."""

import os
import sqlite3
import sys
import unittest
from contextlib import ExitStack
from html.parser import HTMLParser
from unittest import mock


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import report_html


class _Node:
    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = dict(attrs or ())
        self.parent = parent
        self.children = []

    def descendants(self):
        for child in self.children:
            if isinstance(child, _Node):
                yield child
                yield from child.descendants()

    def text(self):
        return "".join(
            child.text() if isinstance(child, _Node) else child
            for child in self.children
        )


class _DOM(HTMLParser):
    _VOID = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self._stack = [self.root]
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs, self._stack[-1])
        self._stack[-1].children.append(node)
        if tag not in self._VOID:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self._stack[-1].children.append(_Node(tag, attrs, self._stack[-1]))

    def handle_endtag(self, tag):
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data):
        self._stack[-1].children.append(data)

    def find_all(self, **attrs):
        return [
            node
            for node in self.root.descendants()
            if all(node.attrs.get(key) == value for key, value in attrs.items())
        ]

    def by_id(self, value):
        matches = self.find_all(id=value)
        return matches[0] if len(matches) == 1 else None


def _contains(ancestor, node):
    current = node
    while current is not None:
        if current is ancestor:
            return True
        current = current.parent
    return False


class TestReportTopLevelTabs(unittest.TestCase):
    def render(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript(db.SCHEMA)
        patches = {
            "paper_section": (
                '<section data-testid="manual-paper-content">'
                "<h2>Paper trade tracker</h2></section>"
            ),
            "auto_strategy_section": (
                '<section data-testid="automatic-paper-content">'
                "<h2>Automatic Top-10 strategy</h2></section>"
            ),
            "pulse_section": '<section data-testid="market-content">Pulse</section>',
            "collection_section": "<section>Collection</section>",
            "picks_section": "<section>Picks</section>",
            "rug_section": "<section>Rugs</section>",
            "surges_section": "<section>Surges</section>",
            "bar_chart": "<svg></svg>",
            "scatter": "<svg></svg>",
        }
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(report_html.db, "latest_rows", return_value=[]))
            stack.enter_context(
                mock.patch.object(
                    report_html.paper_trades, "build_portfolio", return_value={}
                )
            )
            stack.enter_context(
                mock.patch.object(
                    report_html.auto_paper, "build_strategy", return_value={}
                )
            )
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(report_html, name, return_value=value))
            return report_html.build(conn)

    def test_tabs_have_accessible_reciprocal_panel_mapping_and_default_state(self):
        dom = _DOM(self.render())
        tablists = dom.find_all(role="tablist")
        tabs = dom.find_all(role="tab")
        panels = dom.find_all(role="tabpanel")

        self.assertEqual(len(tablists), 1)
        self.assertTrue(
            tablists[0].attrs.get("aria-label")
            or tablists[0].attrs.get("aria-labelledby"),
            "the top-level tablist needs an accessible name",
        )
        self.assertEqual(len(tabs), 2)
        self.assertEqual(len(panels), 2)
        self.assertEqual([tab.tag for tab in tabs], ["button", "button"])
        self.assertEqual(
            [(tab.attrs.get("id"), " ".join(tab.text().split())) for tab in tabs],
            [("scanner-tab", "Market scanner"), ("paper-tab", "Paper trades")],
        )

        self.assertIn(
            "hidden", tablists[0].attrs,
            "tab controls must stay hidden when JavaScript cannot activate them",
        )
        expected = {
            "scanner-tab": ("scanner-panel", "true", "0"),
            "paper-tab": ("paper-panel", "false", "-1"),
        }
        for tab in tabs:
            panel_id, selected, tabindex = expected[tab.attrs["id"]]
            self.assertEqual(tab.attrs.get("aria-controls"), panel_id)
            self.assertEqual(tab.attrs.get("aria-selected"), selected)
            self.assertEqual(tab.attrs.get("tabindex"), tabindex)
            panel = dom.by_id(panel_id)
            self.assertIsNotNone(panel)
            self.assertEqual(panel.tag, "section")
            self.assertEqual(panel.attrs.get("role"), "tabpanel")
            self.assertEqual(panel.attrs.get("aria-labelledby"), tab.attrs["id"])
            self.assertNotIn(
                "hidden", panel.attrs,
                "both panels must remain readable before JavaScript enhancement",
            )

        rendered = self.render()
        self.assertRegex(
            rendered,
            r"classList\.add\(\s*['\"]tabs-ready['\"]\s*\)",
            "JavaScript must mark the enhanced tab state explicitly",
        )
        self.assertRegex(
            rendered,
            r"panels\s*\[\s*index\s*\]\.hidden\s*=\s*!selected",
            "JavaScript, not base HTML, must hide exactly the inactive panel",
        )
        self.assertRegex(
            rendered,
            r"(?:\.hidden\s*=\s*false|removeAttribute\(\s*['\"]hidden['\"]\s*\))",
            "JavaScript must expose the tablist/active panel when enhancement starts",
        )

    def test_paper_sections_render_once_and_only_inside_paper_panel(self):
        rendered = self.render()
        dom = _DOM(rendered)
        scanner_panel = dom.by_id("scanner-panel")
        paper_panel = dom.by_id("paper-panel")
        self.assertIsNotNone(scanner_panel)
        self.assertIsNotNone(paper_panel)

        manual = dom.find_all(**{"data-testid": "manual-paper-content"})
        automatic = dom.find_all(**{"data-testid": "automatic-paper-content"})
        market = dom.find_all(**{"data-testid": "market-content"})
        self.assertEqual(len(manual), 1)
        self.assertEqual(len(automatic), 1)
        self.assertEqual(len(market), 1)
        self.assertTrue(_contains(paper_panel, manual[0]))
        self.assertTrue(_contains(paper_panel, automatic[0]))
        self.assertFalse(_contains(scanner_panel, manual[0]))
        self.assertFalse(_contains(scanner_panel, automatic[0]))
        self.assertTrue(_contains(scanner_panel, market[0]))
        self.assertEqual(rendered.count("Paper trade tracker"), 1)


if __name__ == "__main__":
    unittest.main()
