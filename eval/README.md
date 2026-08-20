# Golden sets

`golden.json` is the working set. 6 questions, each paired with the page that should
answer it.

`golden-v1-mislabeled.json` is the first version, kept on purpose. It expects the
configuration page for "How do I target a specific country for my request?" That's wrong:
the features page documents country targeting. Running it scores 83% hit@5 and names that
question as a miss.

It's here because the lesson is the point. A miss means the corpus and your expectations
disagree, and it doesn't tell you which of the two is wrong. Read the chunk that came
back before you change anything about the pipeline.

    python scripts/5_evaluate.py eval/golden-v1-mislabeled.json --k 5   # 83%, one miss
    python scripts/5_evaluate.py eval/golden.json --k 5                  # 100%
