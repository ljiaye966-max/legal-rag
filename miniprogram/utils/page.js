function requireLogin() {
  return getApp().requireLogin();
}

function isAdmin() {
  const auth = getApp().getAuth();
  return auth && ["kb_admin", "sys_admin"].includes(auth.role);
}

function isSystemAdmin() {
  const auth = getApp().getAuth();
  return auth && auth.role === "sys_admin";
}

module.exports = {
  requireLogin,
  isAdmin,
  isSystemAdmin
};
