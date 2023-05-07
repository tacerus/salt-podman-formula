# vim: ft=sls

{%- set tplroot = tpldir.split("/")[0] %}
{%- from tplroot ~ "/libtofs.jinja" import files_switch with context %}
{%- from tplroot ~ "/map.jinja" import mapdata as podman with context %}
{%- set sls_config_file = tplroot ~ ".package.install" %}
{%- set sls_service_running = "" %}
{%- if podman.containers.values() | selectattr("rootless", "defined") | rejectattr("rootless") | list %}
{%-   set sls_service_running = tplroot ~ ".service.running" %}
{%- endif %}

include:
  - {{ sls_config_file }}
{%- if sls_service_running %}
  - {{ sls_service_running }}
{%- endif %}

{%- for cnt_name, cnt in podman.containers.items() %}
{%-   set rootless = cnt.get("rootless", True) %}
{%-   if rootless %}

User account for container {{ cnt_name }} is present:
  user.present:
    - name: {{ cnt_name }}
    - home: {{ podman.lookup.containers.base | path_join(cnt_name) }}
    - createhome: true
    - usergroup: true
    # (on Debian 11) subuid/subgid are only added automatically for non-system users
    - system: false

User session for container {{ cnt_name }} is initialized at boot:
  compose.lingering_managed:
    - name: {{ cnt_name }}
    - enable: true
    - require:
      - user: {{ cnt_name }}

Podman API for container {{ cnt_name }} is enabled:
  compose.systemd_service_enabled:
    - name: podman.socket
    - user: {{ cnt_name }}
    - require:
      - User session for container {{ cnt_name }} is initialized at boot

Podman API for container {{ cnt_name }} is available:
  compose.systemd_service_running:
    - name: podman.socket
    - user: {{ cnt_name }}
    - require:
      - Podman API for container {{ cnt_name }} is enabled
{%-   endif %}

{%-   if cnt.get("env_secrets") %}

Container {{ cnt_name }} secrets are present:
  podman.secret_present:
    - names:
{%-     for sname, sval in cnt.env_secrets | dictsort %}
      - {{ sname }}:
{%-       if sval is mapping %}
{%-         for param, val in sval.items() %}
        - {{ param }}: {{ val | json }}
{%-         endfor %}
{%-       else %}
        - data: {{ sval | json }}
{%-       endif %}
{%-     endfor %}
{%-     if rootless %}
    - user: {{ cnt_name }}
{%-     endif %}
    - require:
{%-     if rootless %}
      - Podman API for container {{ cnt_name }} is available
{%-     else %}
      - sls: {{ sls_service_running }}
{%-     endif %}
    - require_in:
      - Container {{ cnt_name }} is present
{%-   endif %}

{%-   set secret_env = {} %}
{%-   for secret in cnt.get("env_secrets", {}) %}
{%-     do secret_env.update({secret: secret}) %}
{%-   endfor %}
{%-   do secret_env.update(cnt.get("secret_env", {})) %}

{%-   set labels = {} %}
{%-   if cnt.get("autoupdate") %}
{%-     do labels.update({"io.containers.autoupdate": "registry"}) %}
{%-   endif %}
{%-   do labels.update(cnt.get("labels", {})) %}

Container {{ cnt_name }} is present:
  podman.present:
    - name: {{ cnt_name }}
    - image: {{ cnt.image }}
{%-   if secret_env %}
    - secret_env: {{ secret_env | json }}
{%-   endif %}
{%-   if labels %}
    - labels: {{ labels | json }}
{%-   endif %}
{%-   for cparam, cval in cnt | dictsort %}
{%-     if cparam in ["autoupdate", "env_secrets", "generate_params", "image", "labels", "name", "secret_env", "user"] %}
{%-       continue %}
{%-     endif %}
    - {{ cparam }}: {{ cval | json }}
{%-   endfor %}
{%-   if rootless %}
    - user: {{ cnt_name }}
{%-   endif %}
    - require:
{%-   if rootless %}
      - Podman API for container {{ cnt_name }} is available
{%-   else %}
      - sls: {{ sls_service_running }}
{%-   endif %}

Container {{ cnt_name }} systemd unit is installed:
  file.managed:
    - name: {{ ((podman.lookup.containers.base | path_join(cnt_name, ".config", "systemd", "user")) if rootless else "/etc/systemd/system")
                 | path_join(cnt_name ~ ".service") }}
    - source: {{ files_switch(["container.service.j2"],
                              lookup="Container {{ cnt_name }} systemd unit is installed"
                 )
              }}
    - mode: '0644'
    - user: {{ cnt_name if rootless else "root" }}
    - group: {{ cnt_name if rootless else mariadb.lookup.rootgroup }}
    - makedirs: True
    - template: jinja
    - require:
      - Container {{ cnt_name }} is present
    - context:
        name: {{ cnt_name }}
        generate_params: {{ cnt.get("generate_params", {}) | json }}
        user: {{ cnt_name if rootless else "root" }}

{%-   if rootless %}

Podman autoupdate service is managed for {{ cnt_name }}:
  compose.systemd_service_{{ "enabled" if cnt.get("autoupdate") else "disabled" }}:
    - user: {{ cnt_name }}
    - name: podman-auto-update.timer
    - require:
      - Container {{ cnt_name }} is present
{%-   endif %}
{%- endfor %}
