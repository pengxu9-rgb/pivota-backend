# Copy to config.local.sh (gitignored) and fill in. NEVER commit the filled-in file: the repo is
# public and the owner's rule is no private IPs in committed files.
#
# config.sh PARSES this file and never runs it. Only comment lines, blank lines and exactly these two
# KEY=value lines are accepted, with no quotes, spaces or anything else; any other line is refused.
#
#   SUBNET_RANGE     the Cloud Run subnet for direct VPC egress: an RFC 1918 /24 to /28 (a.b.c.d/nn)
#   PSA_RANGE_START  the first address of the RFC 1918 /20 reserved for Cloud SQL's private IP;
#                    it must be the range sandbox-backend's config/sandbox_identity.py holds (as a
#                    sha256), and must not overlap SUBNET_RANGE or prod's / staging's ranges
SUBNET_RANGE=
PSA_RANGE_START=
