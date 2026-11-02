# Who may do what in the shop: the rule `allow` of `package shop`, which the app queries as
# shop.allow with the input {"subject": ..., "resource": ..., "action": ..., "context": ...}.
package shop

# Nothing is allowed unless a rule below says so: the rule is never undefined.
default allow := false

# Everybody reads the catalogue, signed in or not.
allow if {
	input.action == "GET"
	input.resource == "/products"
}

# Customers read their own orders, and the support staff every order.
allow if {
	input.action == "read"
	input.resource.customer == input.subject.name
}

allow if {
	input.action == "read"
	"support" in input.subject.roles
}

# The support staff refund up to 100 euros; above that, a manager does.
allow if {
	input.action == "refund"
	"support" in input.subject.roles
	input.resource.total <= 100
}

allow if {
	input.action == "refund"
	"manager" in input.subject.roles
}
