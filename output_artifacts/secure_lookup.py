def lookup_url(connection, short_code):
    return connection.execute(
        "SELECT long_url FROM urls WHERE short_code = ?",
        (short_code,),
    ).fetchone()