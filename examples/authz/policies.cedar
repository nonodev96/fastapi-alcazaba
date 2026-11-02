// Everybody with an account lists the documents; each one is checked as it's read.
permit (principal, action == Action::"GET", resource == Resource::"/documents");

// The members of a document's team read it.
permit (principal, action == Action::"read", resource)
when { principal.team == resource.team };

// Its owner reads and edits it.
permit (principal, action in [Action::"read", Action::"edit"], resource)
when { resource.owner == principal };

// Admins delete documents.
permit (
  principal in Role::"admin",
  action == Action::"delete",
  resource == Resource::"documents"
);

// Contractors never see a confidential document, whatever else allows it.
forbid (principal, action, resource)
when { principal.contractor && resource is Document && resource.confidential };
