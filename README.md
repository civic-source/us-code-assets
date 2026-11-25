# Civic Source static assets

This project serves versioned Pagefind and compressed Git comparison assets for
`civic-source/us-code-tracker`. Canonical statute and historical reader URLs stay
in the tracker project. Source code is Apache-2.0; derived statutory material
retains its source licensing. This repository does not accept or certify law.

The publication contract is documented in tracker ADR0014. A protected tracker
workflow publishes an immutable `static-assets-request-<SHA256>` release with
`request.json`, `publication.json`, `main.tar.gz` and `assets.tar.gz`. The consumer
verifies the exact source run attempt, completed `prepare-publication` job, final
authored request-binding step, separate successful main-branch CI, and immutable
release tag. The request source SHA must still equal the tracker protected main
HEAD; the publisher source SHA must equal this repository main HEAD before live
publication. Superseded requests hold rather than roll code or data backward. It checks the live main locator before downloading large archives
and again immediately before deploying assets.

The Python consumer has no package dependencies. It verifies both complete
archives against the externally pinned machine file table and file hashes.
Archives are deterministic, files-only USTAR inside one gzip member; links,
sparse files, extension headers, unsafe paths, duplicate/missing/extra members,
unknown schemas and duplicate JSON keys refuse. Each complete project must fit
900,000,000 logical bytes. Two prior asset generations may coexist with the
candidate. Staging reserves 2 GiB and 10,000 free inodes, with checks before and
during descriptor-pinned writes. Failed stages remain for reconciliation; inputs
and live publication pointers are not overwritten by archive validation.

Fresh online preparation requires the main-only `static-publication-context.json`
marker with exactly `schemaVersion`, `locator`, and `previousLocator`. The live
main writer authorizes the current generation and its distinct predecessor; the
candidate must retain their complete publication bindings. A rebuild of the same
generation preserves its predecessor. Initial publication requires actual 404
responses for both live main metadata files. Successful null responses, missing
context, mismatched bindings, and changed live state hold publication. Offline
archive verification remains compatible with retained older archives.

The existing SHA-pinned internal proof carries the measured `publicationContext`
and `retainedLocators` inside `result`. Preparation measures them from fully
verified archives; predeployment and receipt publication validate them against
fresh live main metadata. Public request, receipt, locator and descriptor schemas
retain their existing fields.

Prepared Pages and proof artifacts use run-and-attempt names. Both publish-job
consumers use the exact names exported by the successful prepare job. Rerunning
all jobs produces new artifact names; rerunning only a failed publish job reuses
the artifacts from its actual preparation attempt.

```sh
python3 -m compileall -q publisher tests
python3 -m unittest discover -s tests -v
```

`publish-assets.yml` polls public release requests on the protected default
branch and supports an optional exact request ID through manual dispatch. It uses
only this repository's `GITHUB_TOKEN`. No PAT, cross-repository write token or
external URL from a request is accepted. Publication is serialized. Set branch
protection and the `github-pages` deployment environment, enable immutable
releases, and configure Pages for Actions before the initial coordinated merge
and deployment. A draft or unknown receipt holds the handshake for reconciliation.

After Pages succeeds, the publisher checks public generation/binding hashes,
Pagefind runtime and one gzip transport sample where present. A separate immutable
`static-assets-ready-<requestId>` release contains `receipt.json`; the final
`publish-assets` job stamps `Bind asset receipt <requestId>`. The tracker verifies
that completed job/run attempt and CI before considering the receipt. Its final
HTTP recheck remains mandatory because two repository deployments are not atomic.

Receipt scope distinguishes complete **main and asset archive** verification from
**sampled HTTPS** readback. It does not claim every public file was requested or
that a source/legal acceptance or main-reader deployment occurred. No secret is
included in artifacts or receipts. Older generations remain recoverable from the
tracker's immutable archives; active generations are not pruned to make an
unverified candidate fit.
