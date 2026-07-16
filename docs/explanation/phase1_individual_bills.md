# Understanding Individual Bills Justification

## Overview

* **What it is:** Most of the files that need justification come from individual bills and their respective payments, so we decided to face these cases first. In this automation, we created a process that renames, matches and merges each bill (*factura*) with its corresponding payment proof (*remesa*), producing a single justification PDF per bill, and reports anything it couldn't process cleanly.
* **Its primary role:** It removes the manual work of cross-referencing an invoice against its proof of payment inside Sharepoint-hosted batches of files, and produces a single, self-contained justification document per bill plus a QA report for the cases that need a human to manage them.

---

## The Problem Space (Why do we need this?)

Justification requests need, for each bill, evidence that it was actually paid, but bills and their payment proofs don't arrive as matched pairs. Payments are grouped into bank remittance batches, and a person had to manually find, inside each batch, the one payment that corresponds to a given bill, then combine the two documents.

* **Volume and structure make manual matching error-prone.** Remittance batches are deeply nested (year -> bank -> specific remittance) and contain many individual payments PDFs, which makes picking the right one by eye tedious.
* **Payments and bills don't share a common filename convention out of the box.** A bill is named around its invoice reference, but a payment file, before this automation, could be named however the bank export happened to name it. Matching them requires normalizing both to the same identifier first.
* **Failures need to be visible.** A bill with no matching payment, an unreadable filename or a corrupted file shouldn't just vanish from the process, someone needs to know it wasn't handled, and why.

---

## Design and Architecture (How we solved it)

The pipeline is built around a single normalized identifier, the **SAP ID**, extracted form both sides independently, so matching doesn't depend on either bill or payment filenames looking alike.
1. **Normalize payment filenames.** Payment files, as they arrive in a remittance batch, get renamed to a canonical `<sap_id>-P.pdf` form by reading the SAP ID out of the PDF's own content.
2. **Match.** Bills are walked from their input folder, each bill's SAP ID is parsed from its filename and looked up against an index built from the normalized payment filenames.
3. **Merge.** A matched bill and payment are merged into a single output PDF, named and filed under a year-based output folder.
4. **Close out.** The payment is renamed again to mark it as processed, and the original bill is removed once the merge is confirmed.
5. **Report.** Anything that didn't fit the happy path, such as not a PDF, unparseable filename, no match found, a failed merge or rename, is logged to a dedicated QA report, uploaded alongside the run's output, and optionally emailed to an admin.

Input files are found in Sharepoint. A local mirror is kept via a dedicated one-way sync client (download-only scoped to the input folder), which the pipeline reads from directly. Anything the pipeline needs to change on Sharepoint's side (renames, uploads, etc.) goes through direct Microsoft Graph API calls, independent of that sync client, since the sync client itself never pushes local changes back up.

### Key Mechanisms

* **SAP ID matching:** both bills and payments are reduces to a shared identifier (SAP ID) via regex extraction, and matched via equality on that identifier rather than on raw filenames. This decouples the match from whatever naming convention either side happens to arrive with.
* **Remote-before-local renaming:** whenever a file needs renaming and has a Sharepoint counterpart, the Sharepoint rename is attempted first. The local rename only happens if that succeeds. This guarantees local and remote state can never silently disagree: a failed Sharepoint rename leaves the local file untouched, so the next run picks it up again instead of the pipeline believing something is done that Sharepoint never saw.
* **Tagged QA logging:** log records that represent something needing follow-up are marked with a dedicated tag, independent of their severity level. A filtered log handler collects only tagged records into a separate QA report file, which becomes both the uploaded artifact and the emailed summary, keeping the errors decoupled from the general operational log.

### Design Decisions

* **Decision:** Match bills to payments via an extracted SAP ID rather than direct filename comparison.
* **Rationale:** Bills and payments never shared a filename convention, anchoring the match to a value derived independently from each side's own content/filename made the pipeline resilient to inconsistent naming across different remittance sources.

* **Decision:** Perform the Sharepoint-side rename before the local rename, not the other way around or in parallel.
* **Rationale:** A rename that succeeds locally but fails remotely leaves two systems permanently out of sync with no clean way to detect it later. Ordering it remote-first menas a failure is always safe to retry, and success always means both sides agree. 

* **Decision:** Keep the QA report as a plain, append-only text log rather than a structured report.
* **Rationale:** It needed to serve two purposes with no extra code: be used as a readable email body, and a file that can be uploaded as-is next to the run's other output. A structured format, on the other hand, would need a rendering step for either use.
---

## Alternatives Considered

* **Relying on the OneDrive sync client to propagate renames back to SharePoint**, instead of calling the Graph API directly: considered implicitly by the sync client being present at all, but it's a **download-only** mirror by design, so it never pushes local changes upstream. Any rename made to the local mirror would never reach SharePoint on its own, which is why the pipeline talks to Graph directly for anything it needs to change remotely.

---

## Trade-offs and Limitations

* **Deleting the source bill on successful merge is irreversible.** Once a bill is merged and marked processed, its original file is removed. If the merge later turns out to be wrong (e.g. filed under an incorrect name), there is no way to regenerate it from the pipeline's own inputs; fixing it requires manual intervention on whatever survived.
* **Matching is entirely SAP-ID-driven — a bill with no correspondingly-tagged payment is simply left unmatched**, not partially reconciled or queued for manual pairing. It shows up in the QA report, but resolving it is a manual step outside the pipeline.
* **The local input mirror is an all-or-nothing sync.** The sync client mirrors its entire configured scope; there's no lightweight or incremental way to pull down just a subset of input data through it, which makes iterating on or testing the pipeline against a small dataset slower than it needs to be without a separate workaround.
* **A failure in an optional, downstream step (e.g. sending the QA report notification) can be indistinguishable from a failure of the core pipeline unless each stage is given its own error boundary.** The pipeline's success/failure signal is only as meaningful as how carefully each stage's exceptions are scoped.

---

## Related Concepts

* **How-to Guide:** [Link to how to implement/use this concept]
* **Tutorial:** [Link to a beginner learning path involving this concept]
* **Reference:** [Link to API/Command reference for this component]