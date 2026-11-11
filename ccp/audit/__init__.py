"""Checking the record against itself (spec.md A-137).

signing       who signs a checkpoint: a local Ed25519 key offline, AWS KMS in production
checkpoints   a signed statement of each tenant's chain head, kept in the database and
              exported outside it (a directory offline; a compliance-mode bucket in a
              separate account in production)
verify        the chain walked, every chained row re-hashed, every checkpoint's signature
              and export checked
"""
