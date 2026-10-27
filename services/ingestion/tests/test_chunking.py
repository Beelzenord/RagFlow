"""The chunker keeps paragraphs - and FAQ questions with their answers - whole.

    cd services/ingestion && python3 -m unittest discover -s tests -t .

Needs tiktoken, so it is skipped where the ingestion dependencies are not
installed; CI and the container run it.
"""
from __future__ import annotations

import unittest

try:
    from rag_shared.chunking import _ENC, chunk_markdown

    DEPS = True
except ImportError:  # pragma: no cover - depends on the environment
    DEPS = False


def para(n: int, words: int = 14) -> str:
    return " ".join(f"word{n}x{i}" for i in range(words)) + "."


@unittest.skipUnless(DEPS, "needs tiktoken and pydantic-settings")
class ChunkingTests(unittest.TestCase):
    SIZE, OVERLAP = 120, 40

    def chunks(self, md: str):
        return chunk_markdown(md, size=self.SIZE, overlap=self.OVERLAP)

    def test_a_section_that_fits_is_one_chunk(self) -> None:
        got = self.chunks("# Purpose\n\nFirst paragraph.\n\nSecond paragraph.")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].heading, "Purpose")
        self.assertEqual(got[0].content, "First paragraph.\n\nSecond paragraph.")

    def test_paragraphs_are_never_cut(self) -> None:
        paragraphs = [para(n) for n in range(12)]
        got = self.chunks("# Rules\n\n" + "\n\n".join(paragraphs))
        self.assertGreater(len(got), 1)
        for c in got:
            self.assertLessEqual(c.token_count, self.SIZE)
            for piece in c.content.split("\n\n"):
                self.assertIn(piece, paragraphs)
        for p in paragraphs:
            self.assertTrue(any(p in c.content for c in got), p)

    def test_a_question_stays_with_its_answer(self) -> None:
        faq = [f"**Q: Question number {n}, about rule {n}?**\nA: " + para(n, 10) for n in range(10)]
        got = self.chunks("## Frequently asked questions\n\n" + "\n\n".join(faq))
        for c in got:
            for block in c.content.split("\n\n"):
                self.assertTrue(block.startswith("**Q:") and "\nA: " in block, block[:40])

    def test_consecutive_chunks_overlap_by_whole_paragraphs(self) -> None:
        # Short paragraphs, so the last one of a chunk fits in the overlap.
        got = self.chunks("# Rules\n\n" + "\n\n".join(para(n, 4) for n in range(30)))
        self.assertGreater(len(got), 2)
        for a, b in zip(got, got[1:]):
            pa, pb = a.content.split("\n\n"), b.content.split("\n\n")
            # The next chunk opens with the previous one's last paragraphs, in
            # order, as many as fit in the overlap.
            k = 0
            while k < len(pb) and pb[k] in pa:
                k += 1
            self.assertGreaterEqual(k, 1)
            self.assertEqual(pa[-k:], pb[:k])
            self.assertLessEqual(sum(len(_ENC.encode(x)) for x in pb[:k]), self.OVERLAP)
            # And every chunk brings something new.
            self.assertTrue(set(pb) - set(pa))

    def test_a_paragraph_bigger_than_the_overlap_is_not_repeated(self) -> None:
        got = self.chunks("# Rules\n\n" + "\n\n".join(para(n) for n in range(12)))
        for a, b in zip(got, got[1:]):
            self.assertFalse(set(a.content.split("\n\n")) & set(b.content.split("\n\n")))

    def test_a_paragraph_too_long_for_a_chunk_is_still_split(self) -> None:
        long = para(1, 300)
        got = self.chunks("# Long\n\n" + long)
        self.assertGreater(len(got), 1)
        for c in got:
            self.assertLessEqual(c.token_count, self.SIZE)

    def test_page_numbers_follow_where_a_chunk_starts(self) -> None:
        md = ("<!-- page: 1 -->\n\n# Rules\n\n" + "\n\n".join(para(n) for n in range(6))
              + "\n\n<!-- page: 2 -->\n\n" + "\n\n".join(para(n) for n in range(6, 12)))
        got = self.chunks(md)
        self.assertEqual(got[0].page_number, 1)
        self.assertEqual(got[-1].page_number, 2)
        for c in got:
            first = c.content.split("\n\n")[0]
            if first.startswith("word") and int(first[4:first.index("x")]) >= 6:
                self.assertEqual(c.page_number, 2, first[:12])

    def test_no_chunk_is_only_repeated_text_when_the_next_paragraph_is_big(self) -> None:
        # A small paragraph that fits the overlap, then one that fills a chunk.
        md = "# Mix\n\n" + "\n\n".join([para(1, 60), para(2, 8), para(3, 60), para(4, 70)])
        got = self.chunks(md)
        seen: set[str] = set()
        for c in got:
            pieces = set(c.content.split("\n\n"))
            self.assertTrue(pieces - seen, c.content[:30])
            seen |= pieces


if __name__ == "__main__":
    unittest.main()
