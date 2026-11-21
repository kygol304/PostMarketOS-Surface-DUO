// Surface Duo port: the session runs inside a chroot, where Linux refuses user namespaces, so Firefox can't use
// its namespace sandbox layer (seccomp filtering still applies). Don't show the warning bar about it every start.
pref("security.sandbox.warn_unprivileged_namespaces", false);
