# Mode brief: data

This mission turns material into structure or numbers: cleaning, sorting, classifying, extracting,
reconciling, aggregating, analysing. Specific expectations:

- **Look at the data before planning.** Head, tail, shape, encoding, delimiter, nulls, a frequency
  table of the key column. Ten lines of `python -c` save an hour. Record what you saw.
- **Define the output schema first** and put it in every brief: column names, types, units, key,
  sort order, file format and path. Members that know the target schema produce mergeable output.
- **Shard by rows or by source, never by column**, when you parallelise. Each `data-sorter` gets a
  slice (file, range, category) and the same schema; you concatenate and de-duplicate. Keep the
  original order recoverable (carry a source-row id).
- **Counts are the verification.** Rows in, rows out, rows dropped and why, duplicates removed,
  nulls per column before and after. Spot-check 5–10 records by hand against the source. A
  transformation without counts is unverified.
- **Classification needs a rubric.** Write the categories with one-line definitions and two
  examples each into the brief; members classify against the rubric, flag "unsure" rather than
  guessing, and report the unsure rate.
- **Numbers get an `analyst`**, who states the method, the assumptions, the population and the
  denominators, and shows the computation (code in `/workspace`, not mental arithmetic).
- **Reasoning gets a `logician`**: whether a conclusion follows from the aggregates, what the
  confounders are, which edge cases the schema does not represent.
- **Deliverables are files**: clean data in `/workspace/data/out/`, the script that produced it
  (re-runnable, idempotent), and a short `README.md` beside them with the schema, the counts and the
  known limitations. The report summarises these and quotes the counts.
- **Never modify the source data in place.** Read from the originals, write to new files.
