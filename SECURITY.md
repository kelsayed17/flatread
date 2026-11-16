# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub:
[Report a vulnerability](https://github.com/kelsayed17/flatread/security/advisories/new). Do not open a public issue.

Include what you found, how to reproduce it, and the impact you expect. You will get an acknowledgement within a
week, and a fix or a plan within 30 days for a confirmed issue.

## Supported versions

Only the latest release receives fixes.

## Scope

flatread reads files that publishers serve, which a caller may not control, so these are in scope:

- a crafted file (a zip, a workbook, XML, HTML or a PDF) that makes a reader exhaust memory or CPU far beyond the
  file's size, such as a decompression bomb or an entity-expansion attack;
- an archive member whose name lets a reader touch a path outside the archive;
- a reader returning a value that differs from the file without raising, when that would let a malicious file pass a
  check silently.
