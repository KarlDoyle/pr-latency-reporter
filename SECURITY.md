# Security

Do not commit GitHub or Slack tokens, database files, or generated reports.
Keep tokens in environment variables and use the narrowest GitHub access that
works for the repositories and teams being measured. The local database and
reports can contain repository and reviewer information; protect them as you
would other internal engineering data.

Please report suspected vulnerabilities privately to the repository owner
through GitHub. Include steps to reproduce and the affected version. Do not
include live credentials or sensitive production data in a report.

The optional `marketplace/` components are experimental scaffolds. Review
their authentication, hosting, and network protections before deploying them.
