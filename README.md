# Pi Backup

A small web dashboard first designed for a Raspberry Pi that replaces the daily manual WinSCP copy:

1. **Unlock and mount** the BitLocker USB disk (password entered in the browser and never saved).
2. **Schedule copy jobs**: which folder on the server's SFTP/FTP server, which days and at what time.
   Each run goes into a new folder named with the date (`/mnt/backup/2026-11-09`).
3. **Watch progress** (percent, speed, time left, files) and see in the **history** whether each copy
   succeeded, with a full log per run.

Copies are done by [rclone](https://rclone.org): it retries dropped connections, and **Retry** on a failed or
interrupted run continues in the same folder, skipping files that are already there.

## Install
```bash
cd ~/pi-backup-script && sudo bash install.sh
```

It installs `python3-flask`, `waitress`, `rclone`, `cryptsetup`, `ntfs-3g`, sets up the `pi-backup` service
(starts at boot), and asks for a dashboard password. Then open `http://<pi-ip>:8080`.
The browser asks you to log in: use any user name and that password.

## First use

1. **Server connection** (bottom of the page): SFTP or FTP, host, port, user, and a password or a private key file on
   the Pi. Press **Test connection**. A PuTTY `.ppk` key (as used by WinSCP) works too: on save it is converted with
   `puttygen` into a root-only OpenSSH copy (`/var/lib/pi-backup/ssh_key`). If the key has a passphrase, type it in
   *Key passphrase*: it is used for that conversion only and never saved. If you replace the `.ppk` later, save the
   connection again.
2. **Disk**: plug in the disk, press Refresh, pick the `BitLocker` partition, enter the password
   (the 48-digit recovery key also works), then **Unlock & mount**.
3. **+ New job**: name, server folder (press **Test server folder** to check it), days and time.
   Keep "Check every file after copying" on: after the copy, rclone compares the file list and sizes with the server,
   and the run is marked **success** only if everything matches.
4. Press **Run now** for a first test, and watch it in **Current copy**.
5. Before unplugging the disk, press **Unmount & lock**.

## Things to know

- **After the Pi restarts, the disk is locked again** and has to be unlocked on the dashboard (by design: the
  password is never stored). A scheduled run while it is locked fails with "The disk is not mounted", so you see it
  in the history.
- A run that was going when the Pi or the service restarted shows as **interrupted**. Press **Retry**: it continues in
  the same folder and skips what is already copied.
- Only one copy runs at a time; others wait in a queue. If a scheduled run comes while the previous run of the same job
  is still going, it is recorded as **skipped**.
- The Pi uses its own clock for schedules. Check the time zone: `timedatectl`, and set it with
  `sudo timedatectl set-timezone Europe/Warsaw` if needed.
- Make sure the Pi itself doesn't reboot automatically for updates (unattended-upgrades `Automatic-Reboot` must be off,
  which is the default).
- The disk must be NTFS or exFAT inside BitLocker (as made by Windows). If mounting says the volume is "dirty" or
  "hibernated", plug the disk into a Windows PC, use *Safely remove*, and turn off Windows Fast Startup.
- The disk is written by Linux and stays readable on Windows as usual.

## Settings

`/etc/pi-backup.env` (restart after editing: `sudo systemctl restart pi-backup`):

| Variable | Default | Meaning |
|---|---|---|
| `PIB_PASSWORD` | (asked by installer) | Dashboard password. Empty = no login. |
| `PIB_ALLOWED_IPS` | (asked by installer) | Computers allowed to open the dashboard: IPs or networks, comma separated, e.g. `192.168.100.21,192.168.100.22` or `10.0.5.0/24`. Others get "Access denied". Empty = any. The Pi itself is always allowed, so an SSH tunnel still works. |
| `PIB_PORT` | `8080` | Web port. |
| `PIB_MOUNT` | `/mnt/backup` | Where the disk is mounted. |
| `PIB_TRANSFERS` | `4` | Files copied in parallel. More can be faster for many small files. |
| `PIB_DATA` | `/var/lib/pi-backup` | Settings (`config.json`), history and logs. |
| `PIB_RCLONE` | `rclone` | rclone binary, e.g. a newer one in `/usr/local/bin/rclone`. |

The server connection is written to `/var/lib/pi-backup/rclone.conf` as the remote `src`, so you can test it by hand:
`sudo rclone --config /var/lib/pi-backup/rclone.conf lsf src:/some/folder`.

Service logs: `journalctl -u pi-backup -f`. Per-run logs: the **Log** button in the history,
or `/var/lib/pi-backup/logs/`.

## Access from other computers

Two ways, pick one:

- **SSH tunnel** (nothing to open): `ssh -L 8080:localhost:8080 pi@<pi-ip>`, then browse to `http://localhost:8080`.
- **Direct** (`http://<pi-ip>:8080`, always available): open port 8080 on the Pi's firewall only for the chosen PCs,
  e.g. `sudo ufw allow from 192.168.100.21 to any port 8080 proto tcp`, and list the same PCs in `PIB_ALLOWED_IPS`
  as a second lock. Give those PCs fixed IPs (DHCP reservations).

## Security notes

- Meant for the internal network only. The dashboard uses plain HTTP with a password, so don't expose it to the internet.
- The SFTP/FTP password is stored in `/var/lib/pi-backup/config.json` (root-only, mode 600) in rclone's *obscured*
  form, which hides it from casual viewing but is **not** encryption. An SSH key (`Private key file`) avoids storing a password.
- The BitLocker password is passed to `cryptsetup` on stdin and never written anywhere.

## Updating

Copy the new files over and run `sudo bash install.sh` again. Settings, jobs and history are kept.
