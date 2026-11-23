# Reading this pull request

The baseline on the target branch records Pi 1.5.1. The dependency downgrade in this PR loses the match at `migrations/main/a.js`. The request, the old native response and the new native response appear in the GitHub Actions job summary.

The three previously accepted diagnostic differences stay visible as a count. They do not cause the PR check to fail.

## Observed transition

```text
Baseline  Pi 1.5.1     migrations/main/a.js:1: console.log("hello");
Downgrade Pi 1.5.0     No matches found
Fixture                glob-root-hit
Request                path=repo  glob=migrations/main/*.js  pattern=hello
```

The restore commit puts the original dependency and lockfile back. The reviewed baseline and the Action revision stay the same across that transition.
