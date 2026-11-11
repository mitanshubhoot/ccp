"""Tenants as data (spec.md A-133): versions, onboarding, and changes of scope.

versions     every version of a tenant's settings, appended; the current one is what runs
onboarding   a new tenant with its first version and its first admin, without a deploy
scope        a change to settings: what decides a status (the GitHub org, the admin limit,
             the critical asset tags, the revocation window, the connections) waits for an
             approver as a scope_change item; the rest an admin records directly
"""
