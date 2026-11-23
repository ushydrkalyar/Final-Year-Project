// The password rule. Must match _check_password_strength in auth.py (tests compare them).

const PASSWORD_HINT = 'At least 8 characters, with letters and numbers';

function passwordProblem(value) {
  const ok = [...value].length >= 8 && /[A-Za-z]/.test(value) && /[0-9]/.test(value);
  return ok ? '' : 'Password must be at least 8 characters and include letters and numbers.';
}

function bindPasswordHint(input, hint) {
  input.addEventListener('input', () => {
    const typed = input.value !== '', bad = typed && passwordProblem(input.value) !== '';
    hint.textContent = typed && !bad ? 'Looks good' : PASSWORD_HINT;
    hint.className = !typed ? 'hint' : bad ? 'hint bad' : 'hint good';
  });
}
