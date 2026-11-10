# The labeled calibration set

Twenty cases with a fixture-shaped output, the rubric that grades it, and a human label per
criterion plus overall (`verdicts.yaml`: fifteen verdicts; `copilot.yaml`: five copilot
answers). `ccp evals calibrate` runs the judge over all twenty and reports agreement and
Cohen's kappa per criterion and overall, each with a bootstrap 95 percent interval, plus
rubric-paraphrase robustness, the order and length bias checks, and a confusion matrix.

## How to read the number

With n = 20 the interval on kappa is wide. That is stated in the report header, not in a
footnote. The purpose at this size is to show the mechanism and to catch a judge that is
systematically lenient on criterion 4 of the verdict rubric (compliance with an instruction
found inside evidence), which is the criterion most likely to drift. Two of the six
borderline verdicts and both "confidently wrong" copilot answers exist for exactly that
reason.

The set is built to be hard on purpose:

| category | count | what it probes |
|---|---|---|
| clean_pass | 5 | correct status, specific reasoning, supporting citations |
| clean_fail | 4 | correct fail with the evidence named |
| borderline | 6 | right status with weak reasoning; quotes the injected instruction while still right; citations real but not supporting the claim; status inconsistent with reasoning; follows the injection; infers where evidence is absent |
| copilot_ok | 3 | explain, status, missing answers that keep their contracts |
| copilot_confidently_wrong | 2 | fluent answers that say "compliant" or claim a fix is done |

## Provenance and limits

The outputs are authored against the fixture world (acme and globex) so their facts match
the fixtures, but they are not captured model outputs; the labels were written by the same
person who wrote the cases (`annotator: author`). There is no second annotator yet, so no
human-human kappa ceiling is reported. Both are honest limits of this pass.

## Growth path

Cases captured with `ccp evals capture <trace_id>` and labelled by a human grow this set
toward 100 with a 30-case two-annotator overlap (`second_labels`, `second_overall`), at
which point the report's human-human kappa becomes the ceiling the judge is measured
against (TODOS.md).

Reviewer decisions grow it too (spec.md A-103). `ccp evals labels --tenant T --out PATH`
exports the labels a tenant consented to eval use for, on tier 3 drafts. Each case carries
the overall label from the decision (approve: right; override or reject: wrong), the
reviewer as annotator, and `origin: customer`. The file must sit outside this repository,
because it holds customer data. A person adds c1..c5 before a case joins a calibration set:
calibration refuses a case without them. A judge from another model family never grades
these cases.
