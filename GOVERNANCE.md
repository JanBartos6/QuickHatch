# QuickHatch Governance

QuickHatch is a source-available project maintained by Jan Bartos.

## Maintainer authority

Jan Bartos is the official creator, maintainer, release manager, and commercial
licensing contact for QuickHatch. External contributors may open issues, feature
requests, forks, and pull requests, but changes are accepted into the official
repository only when Jan Bartos approves and merges them.

## Main branch protection

The repository includes `.github/CODEOWNERS` with Jan Bartos as the owner for
all files. To enforce this on GitHub, configure the `main` branch with:

- require pull request reviews before merging,
- require review from Code Owners,
- restrict who can push to matching branches,
- require status checks before merging,
- include administrators if you want the rule to apply even to admins.

These controls are GitHub repository settings, not source files. The
`CODEOWNERS` file marks ownership; the GitHub branch protection rule enforces
it.

## Licensing and commercial use

The public repository is free for personal, educational, evaluation, and other
non-commercial use under the QuickHatch Source-Available Personal Use License.
Commercial use requires a separate written commercial license from Jan Bartos.
For commercial licensing, contact appdeveloper.honza@gmail.com.

Forks, branches, modified copies, and derivative works must keep the same public
license terms unless Jan Bartos grants a separate written license.

## Contributions

Contributions submitted for inclusion in the official QuickHatch project are
handled under the contribution ownership and license terms in
`CONTRIBUTING.md`.
