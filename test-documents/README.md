# Test documents

Fictional Rahmqvist policies for trying the assistant out: FAQ-style
questions, and how answers follow the **Answers for** market. Every page says
TEST DOCUMENT, every figure is invented, and the contact addresses use the
non-routable `rahmqvist.example` domain.

Upload them on the Dashboard and tag each one as below - or let the answer
evaluation do it: `python3 eval/run.py --seed` (see `eval/README.md`). The
tags it expects are in `eval/fixtures.json`.

| File | Tag as | Good for |
|---|---|---|
| `travel-and-expense-policy.pdf` | Global | Flight class, hotel limits, receipts, when claims are paid |
| `travel-and-expense-addendum-sweden.pdf` | Sweden | Per diem and mileage in SEK - overrides the Global rates in Sweden |
| `it-acceptable-use-and-security-policy.pdf` | Global | Phishing, AI tools, lost devices, USB, passwords |
| `code-of-conduct-gifts-and-conflicts.pdf` | Global | Gift limits, conflicts of interest, side jobs, whistleblowing |
| `learning-and-development-policy.pdf` | Global | Learning budget, learning days, exams, payback |
| `sick-leave-policy-sweden.pdf` | Sweden | Reporting, karensavdrag, medical certificate from day 8, VAB |
| `sick-leave-policy-norway.pdf` | Norway | Self-certification, no waiting day, care days for a sick child |
| `friskvardspolicy-sverige.pdf` | Sweden | Written in Swedish - for questions asked in another language |
| `remote-work-policy-sweden.pdf` | Sweden | Remote days, anchor day, home office allowance |
| `remote-work-policy-norway.pdf` | Norway | The same topics with Norway's rules, for comparing markets |

Questions worth trying:

- **Market-specific beats Global:** "What is the mileage rate for my own car?"
  Sweden gets SEK 30 per mil, everyone else EUR 0.30 per km.
- **One question, two markets:** "Do I get paid on my first sick day?" On All
  markets the answer should name each country's rule.
- **A market with no policy:** "What is the sick pay?" for the United Kingdom
  should say the sources do not cover it, not borrow Sweden's or Norway's.
- **Across languages:** "Can I use the wellness allowance for padel?" in English,
  answered from the Swedish document.

## Regenerating

The eight newer documents come from `generator/` - plain Python with no
dependencies. Edit the text in `generator/policies.py` and run:

```bash
python3 -I test-documents/generator/policies.py test-documents
```

The two remote-work documents were made earlier with a script that is no longer
kept; they are left as they are.
