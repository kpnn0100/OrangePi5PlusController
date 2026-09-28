// Browser sign-in: trades the password for the server's HttpOnly login cookie (CON-03).

export async function login(password) {
  const r = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" },
                                        body: JSON.stringify({ password }) });
  return r.ok;
}
