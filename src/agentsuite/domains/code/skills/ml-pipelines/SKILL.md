---
name: ml-pipelines
description: >-
  Use when building or reviewing a machine learning training pipeline with
  scikit-learn, XGBoost or LightGBM. Covers data leakage, splitting, the Pipeline
  object, cross-validation, and reproducibility - the things that make a model
  score well in the notebook and badly in production.
requires: [ml]
---

# ML pipelines

A model that scores 0.94 offline and 0.71 in production almost always leaked.
Everything below is about preventing that.

## Leakage: the whole ballgame

Leakage is any information in training that will not be available at prediction
time. It inflates your offline score and is invisible unless you look for it.

**Fit on train only. Always. Everything.**

```python
# WRONG -- the scaler has seen the test set
X = scaler.fit_transform(X)
X_train, X_test = train_test_split(X)

# RIGHT -- everything that learns is inside the pipeline, fit inside the split
pipeline = Pipeline([("scale", StandardScaler()), ("model", LogisticRegression())])
X_train, X_test, y_train, y_test = train_test_split(X, y, random_state=0)
pipeline.fit(X_train, y_train)
```

This applies to scaling, imputation, encoding, feature selection, PCA,
oversampling — anything with a `fit`. `Pipeline` exists to make this automatic;
use it rather than remembering.

**Target leakage** is subtler and worse. Ask of every feature: *would I know this
at prediction time?*

Classic offenders:
- `account_closed_date` when predicting churn
- `total_refund_amount` when predicting fraud
- any column derived from the label
- an aggregate computed over the full dataset (`customer_lifetime_value`)
- a timestamp that is only populated after the outcome

A feature with suspiciously high importance is usually leakage, not insight.
Investigate before celebrating.

**Temporal leakage.** If the data has time in it, a random split trains on the
future to predict the past. Split by time:

```python
train = df[df.event_date < "2026-06-01"]
test  = df[df.event_date >= "2026-06-01"]
```

Use `TimeSeriesSplit` for cross-validation, never `KFold`.

**Group leakage.** If one entity appears in many rows (a customer with many
orders), a random split puts the same customer in both sides and the model
memorises them. Use `GroupKFold` / `GroupShuffleSplit` on the entity id.

## The pipeline object

Put every transformation inside it. The pipeline is what gets serialised and
deployed, so anything outside it is a step someone must remember to reproduce.

```python
from sklearn.compose import ColumnTransformer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sklearn.impute import SimpleImputer

numeric = Pipeline([
    ("impute", SimpleImputer(strategy="median")),
    ("scale", StandardScaler()),
])
categorical = Pipeline([
    ("impute", SimpleImputer(strategy="most_frequent")),
    ("encode", OneHotEncoder(handle_unknown="ignore", min_frequency=10)),
])

preprocess = ColumnTransformer([
    ("num", numeric, numeric_columns),
    ("cat", categorical, categorical_columns),
])

model = Pipeline([("prep", preprocess), ("clf", HistGradientBoostingClassifier(random_state=0))])
```

`handle_unknown="ignore"` matters: production will contain categories the
training set never saw, and the default raises.

## Cross-validation

```python
scores = cross_val_score(model, X, y, cv=StratifiedKFold(5, shuffle=True, random_state=0),
                         scoring="average_precision")
print(f"{scores.mean():.3f} +/- {scores.std():.3f}")
```

- Report the **spread**, not just the mean. A model scoring 0.80 ± 0.15 is not
  better than one scoring 0.78 ± 0.01.
- **Stratify** for classification, especially with imbalance.
- Tune hyperparameters with **nested CV** or a held-out validation set. Selecting
  a model on the same folds you report is a form of leakage — the reported score
  is optimistic by construction.
- The **test set is touched once**, at the end. If you looked at it while
  iterating, it is a validation set and you no longer have a test set.

## Baselines

Always establish one before reporting anything:

```python
from sklearn.dummy import DummyClassifier
baseline = cross_val_score(DummyClassifier(strategy="prior"), X, y, cv=cv, scoring=metric)
```

A model at 0.94 accuracy on data that is 94% one class has learned nothing.
Report your score *against* the baseline, and against the simplest reasonable
model (logistic regression, a single tree) — if gradient boosting beats logistic
regression by 0.003, ship the logistic regression.

## Reproducibility

- `random_state` on every estimator, splitter and sampler. Not doing this means
  yesterday's number cannot be reproduced.
- Pin library versions — scikit-learn changes defaults between minor releases.
- Log the data snapshot: row count, date range, and a checksum of the feature
  matrix shape.
- Persist the **whole pipeline**, not the fitted model alone:
  ```python
  joblib.dump(model, "model.joblib")
  ```
  A model without its preprocessing is not deployable.

## Imbalance

Do not reach for SMOTE first. In order:

1. **Use a metric that respects imbalance** (average precision, not accuracy).
2. **`class_weight="balanced"`** — free, and often enough.
3. **Adjust the decision threshold** on the predicted probability. The default
   0.5 is rarely the right operating point.
4. **Resampling** last, inside the CV fold only (`imblearn.pipeline.Pipeline`),
   never on the whole dataset — resampling before splitting duplicates rows
   across the split and leaks directly.

## Before reporting a result

State: the metric and why it was chosen, the baseline, the split strategy, the
spread across folds, and what you checked for leakage. A single accuracy number
with none of that context is not a result.
