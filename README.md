# LUNA Add On Marketplace

The list of GitHub repositories whose Add Ons LUNA shows in Settings > ADD ONS > MARKETPLACE.
LUNA reads [`manifest.json`](manifest.json) and lists every `.lunaaddon` attached to each
repository's latest release.

## Submitting an Add On

1. Make the repository public and publish a release (not a draft or pre-release) with your
   `.lunaaddon` attached.
2. In LUNA, choose **SUBMIT** in Settings > ADD ONS > MARKETPLACE and enter the repository's
   address. LUNA checks the release, then opens a submission issue here, already filled in.
   You can also [open one yourself](../../issues/new?template=submit-add-on.yml).
3. Enter a contact e-mail (the issue is public, so the address is too) and create the issue
   (you need a GitHub account). A check runs on the release and comments on
   the issue. If something is wrong, fix it and edit the issue to check it again.
4. When the check passes, a branch adding your repository is pushed for review. Universal
   Audio reviews it; once it is merged, your Add On appears in LUNA and the issue closes.

Later releases are listed automatically, so you only submit once.

### What is checked

The contact e-mail is an e-mail address. The repository is public and not already listed, and its latest release has at least one
`.lunaaddon`. Each `.lunaaddon` is a zip whose shallowest `manifest.json` (outside `__MACOSX`)
declares at least one Add On with `name`, `id`, exactly one of `html` and `url`, and `width` and
`height` when it opens in a window (the default `target`). The `html` and `icon` files it names
must be inside the `.lunaaddon`, and no two Add Ons in it may share an `id`. Nothing in the
release is run.

Run the same check locally:

```bash
python3 scripts/check_add_on.py https://github.com/<owner>/<repo>
```

## For maintainers

- Each listed repository is one file, `add_ons/<owner>.<repo>.json` (lower case), holding
  `{ "github": "https://github.com/<owner>/<repo>" }`, plus `"private": true` for a repository
  only developers signed in to the GitHub CLI can read. A submitted entry also holds
  `"contact": "<e-mail>"`, the address from the submission issue, so maintainers know whom to
  ask about the Add On; it is left out of `manifest.json`. One file per entry means submission
  pull requests never conflict.
- `manifest.json` is generated: the **Build manifest** workflow rebuilds it from `add_ons/` on
  every push to `main` (`python3 scripts/build_manifest.py`). Don't edit it by hand.
- The **Submit Add On** workflow handles issues labelled `add-on-submission` (the issue form adds
  it): it runs `scripts/check_add_on.py`, then `scripts/push_submission.sh` creates
  `submit/<owner>-<repo>` from `main` with the entry, in a commit whose message closes the issue
  once it reaches `main`, and comments a link that opens the pull request, already filled in.
  The workflow cannot create the pull request itself: the `uaudio-labs` organization does not
  let GitHub Actions create pull requests. Editing the issue checks it again and updates the
  same branch (and its pull request, once open).
- To accept a submission, follow the link in the issue's comment to create the pull request,
  review the Add On, and merge. To decline, close the issue with a comment and delete its
  `submit/` branch.
- To remove an Add On, delete its file from `add_ons/`.
