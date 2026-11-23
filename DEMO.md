# Reading this pull request

The baseline on the target branch records Pi 1.5.1. The dependency downgrade in this PR loses the match at `migrations/main/a.js`. The request, the old native response and the new native response appear in the GitHub Actions job summary.

The three previously accepted diagnostic differences stay visible as a count. They do not cause the PR check to fail.
