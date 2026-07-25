# Batch Details And Preview Review Plan

Status: draft for review
Date: 2026-07-26

---

## Summary

The current Video Details flow works well for a single file, but the real unit of work is usually a set of files in the same folder that share the same stream layout. The proposed design adds a multi-file details workflow that:

- treats one file as the representative source of truth for edits
- finds other files with a matching stream signature inside a chosen scope
- applies the same edit plan across that compatible set
- builds preview copies first
- separates preview management from final replacement

This preserves the existing safety rule: originals are not replaced until the user explicitly accepts previews.

---

## Goals

1. Reduce repeated per-file stream editing when a folder contains the same audio/subtitle/video layout.
2. Preserve the existing preview-before-replace safety model.
3. Make batch results visible and manageable in one place.
4. Avoid applying edits to files that only appear similar by folder name but differ in actual stream layout.

---

## Non-Goals

1. Do not silently replace original files in bulk.
2. Do not assume every file in a folder is compatible.
3. Do not redesign the conversion queue around batch jobs.
4. Do not merge stream editing and conversion into one destructive step.

---

## Current State

The app already has the core pieces needed for this design:

- Single-file stream edit preview, commit, and discard routes.
- Single-file English stereo preview, commit, and discard routes.
- Persisted per-file dropped stream selections in the database.
- A folder-level bulk PGS drop route.
- A details modal that already renders track metadata and workflow actions.

The main gap is that the edit model is still file-scoped, while real usage is often layout-scoped.

---

## Architecture Decision

The batch workflow should be based on a matching stream signature, not on folder membership alone.

Reasoning:

- Folder is a good default scope, but not a safe compatibility guarantee.
- Specials, OVAs, NCOP/NCED files, recap episodes, and damaged files often live beside normal episodes.
- The user should see how many files are compatible before any previews are built.

Proposed rule:

- Scope defaults to the current file's folder.
- The app computes a compatible set by comparing each candidate file's stream signature against the representative file.
- Only compatible files are included in batch preview creation and batch replacement actions.

---

## Recommended User Experience

### Entry Point

The existing Video Details modal gets a new batch mode rather than adding a separate top-level tool.

New controls in the workflow area:

- Apply Same Edits
- Review Matching Files
- Build Batch Previews
- Open Batch Results

### Representative File Model

The file that opened the modal becomes the representative file.

The user edits:

- dropped audio tracks
- dropped subtitle tracks
- English stereo workflow choice

Those choices become the batch edit plan.

### Batch Results Panel

After building previews, the modal exposes a batch result panel showing:

- total files in scope
- compatible files
- excluded files
- previews ready
- preview failures
- replacements accepted
- previews discarded

The panel should support these actions:

- Play representative preview
- Open next ready preview
- Accept this preview
- Accept all ready previews
- Discard one preview
- Discard all previews
- Show excluded files and why they were excluded

This is the key control surface that makes preview and replacement manageable at batch scale.

---

## Batch Workflow

### Phase 1: Build Plan

User edits the representative file in the details modal.

The app creates a batch edit plan from those choices.

Plan contents:

- representative file path
- scope root
- scope mode
- matching signature version
- dropped audio selectors
- dropped subtitle selectors
- whether English stereo preview should be built

### Phase 2: Match Files

The app finds candidate files in scope and compares them to the representative signature.

Each candidate gets one of these outcomes:

- compatible
- excluded: stream count mismatch
- excluded: audio layout mismatch
- excluded: subtitle layout mismatch
- excluded: missing probe metadata
- excluded: file unavailable

### Phase 3: Build Previews

For each compatible file, the app builds the required preview copy or copies.

Rules:

- Apply Same Edits means build previews, not replace originals.
- A file may have a stream-edit preview, an English stereo preview, or both, depending on the plan.
- Failures should not abort the whole batch.

### Phase 4: Review

The user reviews one or more previews and decides whether the batch is acceptable.

Recommended shortcuts:

- Play representative preview first.
- Then sample one or two additional previews.
- If satisfied, accept all ready previews.

### Phase 5: Replace Originals

Replacement remains explicit.

Allowed actions:

- accept a single preview
- accept all ready previews

Replacement behavior should remain consistent with the existing single-file workflows:

- stream-edit replace updates the source file in place
- English stereo replace creates an original backup before replacing
- metadata sync runs after replacement

---

## Stream Signature Design

The stream signature should be deterministic and strict enough to avoid bad batch matches, but not so strict that harmless metadata variation excludes almost everything.

### Proposed Signature Inputs

Representative file signature should include:

- container extension
- video stream presence and codec
- video profile if available
- video resolution
- ordered audio tracks:
  - language tag
  - codec
  - channels
  - normalized title token set
- ordered subtitle tracks:
  - language tag
  - codec
  - normalized title token set

### Title Normalization

Track titles should be normalized before comparison:

- lowercased
- punctuation collapsed
- common noise stripped

This allows small title formatting differences while still distinguishing things like:

- commentary
- signs and songs
- forced
- descriptive audio

### Matching Strictness

Default rule:

- require equal track counts for audio and subtitles
- require the same ordered language and codec layout
- require the same channel count for audio tracks

Possible later relaxation:

- allow subtitle title variation when codec and language match

---

## Edit Plan Representation

The plan should not store raw ffprobe stream indices from the representative file as the only source of truth.

That would be brittle because compatible files may preserve logical track identity while using different absolute stream indices.

Instead, store logical selectors.

### Example Selectors

- drop audio track at ordinal 0 where language is eng and channels is 6
- drop subtitle track at ordinal 1 where codec is PGS and language is jpn
- build English stereo preview from first English audio track

At apply time, each compatible file resolves those selectors against its own streams.

---

## Data Model Proposal

Add a persisted batch-edit plan table or JSON blob model rather than trying to overload `dropped_streams`.

### Option A: Lightweight JSON In SQLite

Add a table such as:

```text
batch_edit_plans
  id
  representative_path
  scope_root
  scope_mode
  signature_version
  plan_json
  created_at
  updated_at
```

And a batch preview result table:

```text
batch_edit_plan_files
  id
  plan_id
  source_path
  match_state
  match_reason
  preview_state
  preview_error
  replace_state
  backup_path
  updated_at
```

This keeps the design explicit and makes the batch result panel cheap to render.

### Option B: In-Memory Only For First Iteration

This is simpler, but weaker.

Tradeoffs:

- easier first implementation
- harder recovery after app restart
- weaker batch result UX

Recommendation: persist the batch plan and per-file batch states in SQLite from the start.

---

## State Machine

Each file in a batch should have its own state machine.

### Match State

- pending
- compatible
- excluded
- errored

### Preview State

- none
- queued
- building
- ready
- failed
- discarded

### Replace State

- not_started
- accepted
- replaced
- failed

This is enough to power the result panel and resume partial work.

---

## API Proposal

### Plan And Matching

- `POST /api/batch_edit_plan/create`
  - input: representative path, scope mode, current single-file edit choices
  - output: plan id, summary, compatible and excluded counts

- `GET /api/batch_edit_plan/<id>`
  - output: plan summary, representative file, compatibility summary, per-file statuses

### Preview Build

- `POST /api/batch_edit_plan/<id>/build_previews`
  - input: optional subset filter
  - output: accepted job summary

- `POST /api/batch_edit_plan/<id>/discard_previews`
  - input: one file or all ready previews

### Replacement

- `POST /api/batch_edit_plan/<id>/accept_preview`
  - input: one file

- `POST /api/batch_edit_plan/<id>/accept_all_ready`
  - input: none or optional subset

### Review Navigation

- `GET /api/batch_edit_plan/<id>/next_preview`
  - output: next preview-ready file path and summary

These endpoints should wrap the existing single-file preview and commit helpers rather than duplicating ffmpeg logic.

---

## UI Proposal

### Details Modal Additions

The workflow section gets a new batch card with:

- scope selector
  - this file only
  - matching files in folder
  - all files in folder
- compatibility summary
- Apply Same Edits button
- Build Batch Previews button
- Open Batch Results button

### Batch Results Panel Contents

- progress summary chips
- ready / failed / excluded counts
- table of affected files
- reason column for excluded and failed entries
- quick actions per row:
  - play preview
  - open folder
  - accept
  - discard

### Visual Status

Queue rows should eventually show small badges when a file has:

- batch preview ready
- batch preview failed
- batch replacement pending

This is optional for phase 1, but recommended soon after.

---

## Backend Reuse Strategy

The new batch layer should reuse existing primitives:

- stream edit preview creation
- stream edit preview commit
- stream edit preview discard
- English stereo preview creation
- English stereo preview commit
- English stereo preview discard
- metadata sync after replacement

The batch system should orchestrate these per file, not fork their logic.

---

## Failure Handling

Batch operations should be best-effort.

Rules:

- one file failure must not abort the batch
- every failed file needs a visible reason
- excluded files are not failures; they are explicit non-participants
- replacements should only run for preview-ready files

For batch replacement, the operation summary should clearly report:

- replaced
- failed to replace
- skipped because preview missing

---

## Phased Delivery

### Phase 1: Batch Plan And Preview Management

- add stream signature matching
- add plan persistence
- add Apply Same Edits for matching files in folder
- build previews only
- add batch result panel

This phase delivers the core user value while preserving current preview safety.

### Phase 2: Batch Replacement Actions

- add accept one
- add accept all ready
- add discard one
- add discard all
- add replacement result summary

### Phase 3: Queue Integration

- add row badges for batch states
- add filters for preview ready, preview failed, excluded
- add folder-level resume entry points

### Phase 4: Advanced Matching And Repair Presets

- add saved repair presets
- allow fuzzy matching options where safe
- allow batch English stereo workflows as first-class repair plans

---

## Open Questions

1. Scope options: phase 1 will support `matching files in folder` only.

2. Representative edits: phase 1 will allow one combined plan with dropped-stream edits plus English stereo preview.

3. Preview sampling: phase 1 will use playback from the results table (no dedicated guided sampler flow yet).

4. Replacement semantics: stream-edit batch replace will also create `.original-backup` files for safety.

5. Excluded files: excluded files remain locked out in phase 1 (no force-include override).

6. Matching strictness: subtitle title mismatches warn but do not exclude when codec, language, and order still match.

7. Progress model: batch preview creation will run as a background job.

8. Persistence: batch plans and per-file batch states will persist from phase 1.

---

## Recommended Initial Answers

These defaults are now approved for implementation:

1. Phase 1 scope: `matching files in folder` only.
2. Allow one combined plan that can include dropped-stream edits plus English stereo preview.
3. Results table playback is enough for phase 1.
4. Create `.original-backup` for stream-edit replacement as well as English stereo replacement.
5. Excluded files stay excluded in phase 1.
6. Subtitle title mismatches warn, but do not exclude if codec, language, and track order still match.
7. Batch preview creation should be a background job.
8. Persist plans and per-file states from phase 1.

---

## Recommendation

Proceed with a multi-file details workflow centered on:

- representative file edits
- matching-file detection inside the folder
- preview-first batch application
- a dedicated batch result panel

This preserves the app's current safety guarantees and scales the existing workflow instead of replacing it.