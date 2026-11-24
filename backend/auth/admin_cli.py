"""Local, interactive admin bootstrap/recovery. Never expose this through an API."""
def register_admin_cli(app):
    import click
    from contextlib import closing
    from werkzeug.security import generate_password_hash
    from tools.db_tool import get_connection
    from tools.admin_service import audit
    from auth.account_service import validate_user_id, normalize_email

    @app.cli.command('admin-account')
    @click.option('--recover',is_flag=True,help='Replace the password of an existing administrator.')
    def admin_account(recover):
        """Create/recover an ADM account with hidden interactive password prompts."""
        uid=click.prompt('Admin ID (ADM prefix)').strip()
        validate_user_id(uid,'admin')
        name=click.prompt('Display name').strip()
        if not 2<=len(name)<=100: raise click.ClickException('Name must contain 2–100 characters.')
        email=normalize_email(click.prompt('Registered email'))
        password=click.prompt('Password (12+ characters)',hide_input=True,confirmation_prompt=True)
        if not 12<=len(password)<=128: raise click.ClickException('Use 12–128 characters.')
        with closing(get_connection()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT role FROM users WHERE user_id=?',(uid,)).fetchone()
            if recover:
                if not row or row['role']!='admin': raise click.ClickException('Administrator not found.')
                conn.execute('UPDATE users SET username=?,email=?,password_hash=?,is_active=1,token_version=token_version+1 WHERE user_id=?',(name,email,generate_password_hash(password),uid))
            else:
                if row: raise click.ClickException('Account already exists. Use --recover if appropriate.')
                conn.execute("INSERT INTO users(user_id,username,email,password_hash,role) VALUES(?,?,?,?,'admin')",(uid,name,email,generate_password_hash(password)))
            audit(conn,uid,'admin.recover' if recover else 'admin.bootstrap',uid,'Local interactive CLI',None,{'role':'admin'})
        click.echo('Administrator account ready. Sign in using the existing login page.')
