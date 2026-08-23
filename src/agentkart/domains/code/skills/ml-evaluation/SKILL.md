---
name: ml-evaluation
description: >-
  Use when choosing a metric, interpreting a model's score, comparing models, or
  deciding whether a result is real. Covers metric selection, thresholds,
  calibration, error analysis and the ways a good-looking number misleads.
requires: [ml]
---

# Evaluating a model

The metric is a modelling decision, not a reporting detail. Choosing the wrong
one produces a model that optimises the wrong thing and looks fine doing it.

## Choosing the metric

Start from the decision the model informs and what an error costs.

| Situation | Use | Not |
|---|---|---|
| Imbalanced classification | average precision (PR-AUC) | accuracy, ROC-AUC |
| Ranking / retrieval | NDCG, recall@k, MRR | accuracy |
| Cost-asymmetric errors | expected cost with your own weights | F1 |
| Probability feeds a decision | log loss + calibration | accuracy |
| Balanced classification | ROC-AUC, accuracy | — |
| Regression, outliers matter | RMSE | MAE |
| Regression, outliers are noise | MAE, MAPE | RMSE |

**Accuracy on imbalanced data is meaningless.** At 1% positives, predicting "no"
always gives 99%.

**ROC-AUC flatters imbalanced problems.** With few positives, a large absolute
number of false positives barely moves the false-positive rate. PR-AUC does not
hide this; use it when the positive class is rare and is the one you care about.

**F1 hides the trade-off** by fixing it at equal weight. If a false negative
costs 20× a false positive — and it usually does — say so explicitly with
`fbeta_score(beta=...)` or a cost matrix.

## The threshold is a separate decision

A classifier outputs a probability. `0.5` is a default, not a choice.

```python
from sklearn.metrics import precision_recall_curve
precision, recall, thresholds = precision_recall_curve(y_val, model.predict_proba(X_val)[:, 1])
```

Pick the threshold on the **validation** set, from the operating point the
business actually needs ("we can review 200 cases a day" → the threshold giving
200 positives), then report test metrics at that fixed threshold. Tuning the
threshold on the test set is leakage.

## Calibration

If the probability feeds a decision — expected value, a queue, a price — it needs
to *mean* something. A model can rank perfectly and be badly calibrated.

```python
from sklearn.calibration import CalibratedClassifierCV, calibration_curve
```

Tree ensembles are typically overconfident at the extremes. `CalibratedClassifierCV`
with `method="isotonic"` (enough data) or `"sigmoid"` (less) fixes it. Check with
a reliability curve: if the model says 0.8, roughly 80% of those cases should be
positive.

If you only ever threshold the score, calibration does not matter. Say which
case you are in.

## Error analysis beats another model

When a model is at 0.85, look at what it gets wrong before reaching for a bigger
one.

1. **Confusion matrix**, always, not just the headline number.
2. **Slice the metric** by segment — region, tenure, channel, time. Aggregate
   performance routinely hides one segment performing terribly. A model that is
   fine overall and useless for new customers is a broken model.
3. **Read actual misclassified rows.** Twenty of them will usually tell you the
   feature that is missing, or the label that is wrong.
4. **Check the label.** A meaningful fraction of "model errors" are annotation
   errors, particularly at the boundary.

## Comparing models honestly

- Compare on the **same folds** with the same seed. Different splits are not a
  comparison.
- Report the **spread**. A 0.005 difference with a fold standard deviation of
  0.03 is noise, and claiming it as an improvement is wrong.
- Include **cost**: training time, inference latency, memory, and the
  maintenance burden of another dependency. A 0.3% gain rarely pays for a model
  nobody can debug at 3am.
- **Prefer the simpler model** on a tie. Explicitly.

## Statements that need evidence

Do not write these unless you have the number in front of you:

- "The model performs well" — against what baseline, on what metric?
- "This feature is important" — importance from what method? Permutation
  importance on held-out data, or a train-set impurity measure that rewards
  high-cardinality columns?
- "It generalises" — to which distribution? Tested on which period?
- "Accuracy improved" — beyond fold variance?

## What to report

> **Model:** gradient boosting, 47 features, trained on 2025-01 → 2026-05,
> tested on 2026-06 → 2026-08 (temporal split).
>
> **Metric:** average precision, because positives are 2.3% and the cost of a
> missed case dominates. AP **0.41** vs prior-rate baseline **0.023** and
> logistic regression **0.38**.
>
> **Operating point:** threshold 0.31, chosen for a 200/day review capacity.
> At that point: precision 0.34, recall 0.58.
>
> **Caveats:** performance on customers with under 30 days of history is AP 0.12
> — effectively unusable for that segment, which is 18% of volume. `days_since_signup`
> is the top feature by permutation importance; worth confirming it is available
> at scoring time.
