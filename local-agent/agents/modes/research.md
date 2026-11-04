# Mode brief: research

This mission is an investigation whose deliverable is knowledge: an answer, a comparison, a
briefing, a dataset of facts with sources. Specific expectations:

- **Decompose the question before searching.** Write the sub-questions as plan steps. Each
  `researcher` gets one sub-question, the criteria for a good answer, and the rule that every claim
  needs a URL whose extracted text actually supports it.
- **Parallelise by question, not by engine.** Three members on three sub-questions is good; three
  members all searching the same thing is waste.
- **Local search is slow and modest.** SearXNG aggregates public engines through the proxy;
  Crawl4AI extracts pages. Budget ~30–60 s per search and ~10–30 s per page. Brief members to run
  *few, well-phrased* searches and to read full pages rather than trusting snippets.
- **Conflicts are findings.** When sources disagree, the report says they disagree, with both
  values and both URLs, and gives the reason you prefer one (primary source, recency, specificity).
  Never average, never pick silently.
- **Audit the result.** Run a `logician` over the assembled claims before synthesis: unsupported
  claims, circular sourcing (two pages quoting the same original), dated information, numbers
  compared on different bases.
- **The owner's own documents count as sources.** Use `rag_search` (or brief a `librarian`) for
  anything the owner may already have.
- **Write the synthesis yourself** (or brief a `writer` with the verified claim list): lead with
  the answer, then the evidence, then the uncertainty, then a source list with one line per URL on
  what it contributed. Prefer tables for comparisons.
- **Save artifacts.** Put the final report in `/workspace/research/<slug>.md` and the raw claim
  list with URLs beside it, so the owner can reuse them.
