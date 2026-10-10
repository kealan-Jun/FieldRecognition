"use strict";
document.querySelector("form").onsubmit = async (e) => {
  e.preventDefault();
  let data = Object.fromEntries(new FormData(e.target));
  try {
    let r = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    let j = await r.json();
    if (!r.ok) throw Error(j.detail || j.error?.message || "登录失败");
    sessionStorage.setItem("field_csrf", j.csrf_token);
    location.replace("/");
  } catch (err) {
    document.querySelector("#error").textContent = err.message;
  }
};
