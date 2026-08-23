---
name: rag-evaluation
description: >-
  Use when asked whether a RAG system is any good, or before and after changing
  one. Covers building an evaluation set, retrieval metrics, groundedness and
  hallucination checks, and why vibes-based iteration goes backwards.
requires: [rag]
---

# Evaluating RAG

Without measurement, RAG iteration is guesswork: a change fixes the three
examples you tried and breaks nine you did not. Build the evaluation set first.
It takes an afternoon and pays for itself immediately.

## Evaluate the two stages separately

An end-to-end score cannot tell you *where* the system failed, so it cannot tell
you what to fix.

| Stage | Question | Metrics |
|---|---|---|
| Retrieval | Did the right chunk come back? | recall@k, MRR, NDCG |
| Generation | Given that context, was the answer right and supported? | groundedness, correctness |

Retrieval first, always. A perfect generator cannot rescue missing context.

## The evaluation set

50–200 question/answer pairs beats a thousand synthetic ones. Build it from:

- **Real user questions** if you have logs. Nothing else is as representative.
- **Questions written from the corpus** — pick a chunk, write the question it
  answers. Record the chunk id as ground truth.
- **Deliberately hard cases**: multi-hop questions, questions whose answer is not
  in the corpus, near-duplicate topics, exact-identifier lookups.

Include **unanswerable questions**. A system that never says "I don't know" is a
system that hallucinates, and you cannot detect that without testing for it.

```python
@dataclass
class EvalCase:
    question: str
    relevant_chunk_ids: set[str]     # ground truth for retrieval
    expected_answer: str | None      # None means "should refuse"
```

## Retrieval metrics

```python
def recall_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 1.0
    return len(set(retrieved[:k]) & relevant) / len(relevant)

def reciprocal_rank(retrieved: list[str], relevant: set[str]) -> float:
    for rank, doc_id in enumerate(retrieved, start=1):
        if doc_id in relevant:
            return 1.0 / rank
    return 0.0
```

- **recall@k** is the one that matters most: if the chunk is not in the top k, the
  generator never sees it. Measure at your actual k.
- **MRR** tells you whether the right chunk is near the top, which matters because
  of position effects in long contexts.
- Report **recall@5 and recall@20** together. A large gap means reranking will help.

## Groundedness

Is every claim in the answer supported by the retrieved context? This is the
hallucination measure, and it is checkable without a human:

1. Split the answer into atomic claims.
2. For each, ask a model: *is this claim supported by the context? yes / no / partial.*
3. Groundedness = supported claims / total claims.

Judge with a strong model, and **give it only the context and the claim** — not
the question, and not its own prior answer. A judge that can see the question
tends to reason from its own knowledge instead of checking the context.

Validate the judge on 20 hand-labelled examples before trusting its numbers.

## Correctness

Groundedness says the answer follows from the context. Correctness says the
answer is right. Both are needed: a system can be perfectly grounded in a
retrieved chunk that does not answer the question.

For factual answers, exact or fuzzy match against the expected answer. For open
answers, an LLM judge with a rubric — and report agreement with a human sample.

## Refusal behaviour

For unanswerable questions, the correct answer is a refusal. Measure it:

- **Refusal rate on unanswerable questions** — should be high.
- **Refusal rate on answerable questions** — should be near zero.

A system that scores well on answerable questions and refuses nothing is trading
silent hallucination for apparent helpfulness. That trade is usually wrong, and
it is invisible unless you test for it.

## Running the evaluation

```python
def evaluate(system, cases: list[EvalCase]) -> dict[str, float]:
    results = []
    for case in cases:
        retrieved = system.retrieve(case.question)
        answer = system.generate(case.question, retrieved)
        results.append({
            "recall@5": recall_at_k([c.id for c in retrieved], case.relevant_chunk_ids, 5),
            "mrr": reciprocal_rank([c.id for c in retrieved], case.relevant_chunk_ids),
            "grounded": groundedness(answer, retrieved),
            "refused": is_refusal(answer),
        })
    return aggregate(results)
```

Rules that keep this honest:

- **Fix the seed and temperature** (0 for evaluation). Otherwise you are measuring
  noise.
- **Run before and after every change**, and record both. One number in isolation
  says nothing.
- **Version the corpus and the index.** A score is not comparable across a
  re-index.
- **Look at the regressions**, not just the mean. A change that lifts the average
  while breaking exact-identifier lookups is usually a bad change.

## Reporting

> **Change:** added BM25 and RRF fusion alongside dense retrieval.
>
> | metric | before | after |
> |---|---|---|
> | recall@5 | 0.61 | **0.78** |
> | recall@20 | 0.84 | 0.86 |
> | MRR | 0.44 | **0.62** |
> | groundedness | 0.81 | 0.83 |
> | refusal on unanswerable | 0.55 | 0.58 |
>
> The gain is concentrated in questions containing product codes (recall@5 0.31 →
> 0.79); natural-language questions are unchanged, as expected.
>
> recall@20 barely moved while recall@5 jumped — the right chunks were already
> being retrieved, just ranked poorly. Reranking is the next thing worth trying.
>
> Refusal on unanswerable questions is still only 0.58. Roughly 40% of the time
> the system answers from weak context rather than declining. That is the largest
> remaining risk and is not addressed by this change.
