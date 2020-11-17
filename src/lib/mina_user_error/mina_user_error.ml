exception Mina_user_error of {message: string; where: string option}

let raisef ?where =
  Format.ksprintf (fun message -> raise (Mina_user_error {message; where}))

let raise ?where message = raise (Mina_user_error {message; where})
