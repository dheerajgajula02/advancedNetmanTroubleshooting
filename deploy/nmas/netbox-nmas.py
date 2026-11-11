# Extra NetBox settings for the public front end at https://nmas.shiplabs.net/ipam/
# (container netbox-public, deploy/nmas/netbox-public.compose.yml). Mounted into
# /etc/netbox/config/, where the image loads every .py file after configuration.py.
#
# The sign-in is Netman's (lab4_gui/access.py). nginx on the GCP proxy asks Netman's
# /auth/verify first and then passes the account in a header whose name is a shared
# secret (REMOTE_AUTH_HEADER, from the private env file), so NetBox asks no password.
BASE_PATH = 'ipam/'
