# QuickHatch Governance

QuickHatch is an open-source project released under the MIT License and
maintained by Jan Bartos.

## Maintainer authority

Jan Bartos is the official creator, maintainer, and release manager for
QuickHatch. External contributors may open issues, feature requests, forks, and
pull requests, but changes are accepted into the official repository only when
Jan Bartos approves and merges them.

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

QuickHatch's own code and documentation are released under the [MIT
License](LICENSE), including permission for commercial use, modification,
distribution, and sublicensing. Copies or substantial portions must retain the
copyright and permission notices. No separate commercial license is required
for this MIT-licensed work.

Third-party dependencies and content retain their own licenses and attribution
requirements, as described in [README.md](README.md#license-forks-and-contributions).

## Contributions

Contributions submitted for inclusion in the official QuickHatch project are
handled under the contribution ownership and license terms in
`CONTRIBUTING.md`.
