# OpenMontage production and identity

These terms distinguish the person operating OpenMontage from the space that owns a production.

## Language

**User**: A person with a verified identity. The same user can belong to several organizations.
_Avoid_: tenant, company account

**Organization**: An enterprise whose members share production resources according to their roles.
_Avoid_: personal organization, user

**Workspace**: The personal or organization space selected for an operation. A personal workspace belongs to one user; an organization workspace belongs to the organization.
_Avoid_: current company, execution directory

**Owner**: The user or organization to which a production resource belongs. Ownership persists when the creator leaves an organization.
_Avoid_: creator, actor

**Creator**: The user who originally submitted a production resource.
_Avoid_: owner

**Actor**: The verified user responsible for a particular accepted operation, including an approval or membership change.
_Avoid_: client-provided approver, worker user

**Gate**: A specific production or cost decision requiring approval bound to the displayed evidence.
_Avoid_: login permission, generic consent
