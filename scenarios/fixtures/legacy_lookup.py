def lookup_url(connection, short_code):
    query = f"SELECT long_url FROM urls WHERE short_code = '{short_code}'"
    return connection.execute(query).fetchone()