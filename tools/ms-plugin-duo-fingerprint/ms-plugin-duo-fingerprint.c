/*
 * Phosh Mobile Settings device panel for the Surface Duo: fingerprints and screen lock.
 *
 * Fronts the duo-fingerprint command (which drives the FPC TrustZone app, see docs/FINGERPRINT.md):
 *   list / enroll / delete ID, plus two switches:
 *   - fingerprint unlock   -> /etc/duo/fingerprint-unlock-off (absent = on)
 *   - automatic lock        -> /etc/duo/autolock + org.gnome.desktop.screensaver lock-enabled
 *
 * Build (on the phone): see build.sh next to this file.
 */
#define G_LOG_DOMAIN "ms-plugin-duo-fingerprint"

#include "mobile-settings-plugin.h"

#include <adwaita.h>
#include <gio/gio.h>
#include <glib/gstdio.h>
#include <signal.h>
#include <string.h>

#define TOOL      "/usr/local/bin/duo-fingerprint"
#define UNLOCK_OFF "/etc/duo/fingerprint-unlock-off"
#define AUTOLOCK  "/etc/duo/autolock"
#define MAX_FINGERS 5

#define MS_TYPE_DUO_FP_PANEL (ms_duo_fp_panel_get_type ())
G_DECLARE_FINAL_TYPE (MsDuoFpPanel, ms_duo_fp_panel, MS, DUO_FP_PANEL, MsPluginPanel)

struct _MsDuoFpPanel {
  MsPluginPanel        parent;

  AdwSwitchRow        *unlock_row;
  AdwSwitchRow        *autolock_row;
  AdwPreferencesGroup *fingers_group;
  GPtrArray           *finger_rows;
  AdwButtonRow        *add_row;
  AdwActionRow        *status_row;
  GtkButton           *cancel_btn;

  GSubprocess         *enroll;
  GDataInputStream    *enroll_out;
  guint                n_fingers;
  gboolean             busy;
};

G_DEFINE_TYPE (MsDuoFpPanel, ms_duo_fp_panel, MS_TYPE_PLUGIN_PANEL)

static void refresh_list (MsDuoFpPanel *self);


static void
set_busy (MsDuoFpPanel *self, gboolean busy)
{
  self->busy = busy;
  gtk_widget_set_sensitive (GTK_WIDGET (self->add_row), !busy && self->n_fingers < MAX_FINGERS);
  for (guint i = 0; i < self->finger_rows->len; i++)
    gtk_widget_set_sensitive (g_ptr_array_index (self->finger_rows, i), !busy);
}


static void
show_status (MsDuoFpPanel *self, const char *title, const char *subtitle, gboolean cancellable)
{
  gtk_widget_set_visible (GTK_WIDGET (self->status_row), title != NULL);
  if (title)
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (self->status_row), title);
  adw_action_row_set_subtitle (self->status_row, subtitle ? subtitle : "");
  gtk_widget_set_visible (GTK_WIDGET (self->cancel_btn), cancellable);
}


/* ------------------------------------------------------------------ switches */

static void
on_unlock_toggled (MsDuoFpPanel *self)
{
  if (adw_switch_row_get_active (self->unlock_row))
    g_unlink (UNLOCK_OFF);
  else
    g_file_set_contents (UNLOCK_OFF, "", 0, NULL);
}


static void
on_autolock_toggled (MsDuoFpPanel *self)
{
  gboolean on = adw_switch_row_get_active (self->autolock_row);
  g_autoptr (GSettings) s = g_settings_new ("org.gnome.desktop.screensaver");

  if (on)
    g_file_set_contents (AUTOLOCK, "", 0, NULL);
  else
    g_unlink (AUTOLOCK);
  g_settings_set_boolean (s, "lock-enabled", on);
}


/* ------------------------------------------------------------------ list / delete */

static void
on_delete_response (AdwAlertDialog *dialog, const char *response, gpointer data)
{
  MsDuoFpPanel *self = MS_DUO_FP_PANEL (data);
  const char *id = g_object_get_data (G_OBJECT (dialog), "finger-id");
  g_autoptr (GError) err = NULL;
  g_autofree char *out = NULL;
  const char *argv[] = { TOOL, "delete", id, NULL };

  if (g_strcmp0 (response, "delete") != 0)
    return;

  show_status (self, "Removing fingerprint…", NULL, FALSE);
  /* short operation (< 2 s): run synchronously */
  if (!g_spawn_sync (NULL, (char **) argv, NULL, G_SPAWN_DEFAULT, NULL, NULL, &out, NULL, NULL, &err))
    show_status (self, "Could not remove the fingerprint", err->message, FALSE);
  else if (strstr (out, "SAVE FAILED"))
    show_status (self, "Could not save the change", NULL, FALSE);
  else
    show_status (self, NULL, NULL, FALSE);
  refresh_list (self);
}


static void
on_delete_clicked (GtkButton *btn, MsDuoFpPanel *self)
{
  AdwDialog *dialog = adw_alert_dialog_new ("Remove fingerprint?",
                                            "This finger will no longer unlock the phone.");

  adw_alert_dialog_add_responses (ADW_ALERT_DIALOG (dialog), "cancel", "_Cancel", "delete", "_Remove", NULL);
  adw_alert_dialog_set_response_appearance (ADW_ALERT_DIALOG (dialog), "delete", ADW_RESPONSE_DESTRUCTIVE);
  adw_alert_dialog_set_close_response (ADW_ALERT_DIALOG (dialog), "cancel");
  g_object_set_data_full (G_OBJECT (dialog), "finger-id",
                          g_strdup (g_object_get_data (G_OBJECT (btn), "finger-id")), g_free);
  g_signal_connect (dialog, "response", G_CALLBACK (on_delete_response), self);
  adw_dialog_present (dialog, GTK_WIDGET (self));
}


static void
on_list_done (GObject *source, GAsyncResult *res, gpointer data)
{
  MsDuoFpPanel *self = MS_DUO_FP_PANEL (data);
  g_autofree char *out = NULL;
  g_autoptr (GError) err = NULL;
  g_auto (GStrv) lines = NULL;
  guint n = 0;

  for (guint i = 0; i < self->finger_rows->len; i++)
    adw_preferences_group_remove (self->fingers_group, g_ptr_array_index (self->finger_rows, i));
  g_ptr_array_set_size (self->finger_rows, 0);

  if (!g_subprocess_communicate_utf8_finish (G_SUBPROCESS (source), res, &out, NULL, &err)) {
    show_status (self, "Fingerprint service not available", err->message, FALSE);
    g_object_unref (self);
    return;
  }

  lines = g_strsplit (out, "\n", -1);
  for (guint i = 0; lines[i]; i++) {
    const char *l = g_strstrip (lines[i]);
    GtkWidget *row, *btn;
    g_autofree char *title = NULL, *subtitle = NULL;

    if (!g_str_has_prefix (l, "finger "))
      continue;
    n++;
    title = g_strdup_printf ("Fingerprint %u", n);
    subtitle = g_strdup_printf ("ID %s", l + strlen ("finger "));
    row = adw_action_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row), subtitle);
    adw_action_row_add_prefix (ADW_ACTION_ROW (row), gtk_image_new_from_icon_name ("auth-fingerprint-symbolic"));
    btn = gtk_button_new_from_icon_name ("user-trash-symbolic");
    gtk_widget_set_valign (btn, GTK_ALIGN_CENTER);
    gtk_widget_add_css_class (btn, "flat");
    gtk_widget_set_tooltip_text (btn, "Remove");
    g_object_set_data_full (G_OBJECT (btn), "finger-id", g_strdup (l + strlen ("finger ")), g_free);
    g_signal_connect (btn, "clicked", G_CALLBACK (on_delete_clicked), self);
    adw_action_row_add_suffix (ADW_ACTION_ROW (row), btn);
    adw_preferences_group_add (self->fingers_group, row);
    g_ptr_array_add (self->finger_rows, row);
  }
  self->n_fingers = n;
  adw_preferences_group_set_description (self->fingers_group,
                                         n ? NULL : "No fingerprints yet. Add one to unlock with your finger.");
  /* keep the add row last */
  g_object_ref (self->add_row);
  adw_preferences_group_remove (self->fingers_group, GTK_WIDGET (self->add_row));
  adw_preferences_group_add (self->fingers_group, GTK_WIDGET (self->add_row));
  g_object_unref (self->add_row);
  set_busy (self, FALSE);
  g_object_unref (self);
}


static void
refresh_list (MsDuoFpPanel *self)
{
  g_autoptr (GError) err = NULL;
  g_autoptr (GSubprocess) p = g_subprocess_new (G_SUBPROCESS_FLAGS_STDOUT_PIPE | G_SUBPROCESS_FLAGS_STDERR_SILENCE,
                                                &err, TOOL, "list", NULL);
  if (!p) {
    show_status (self, "Fingerprint tool missing", err->message, FALSE);
    return;
  }
  set_busy (self, TRUE);
  g_subprocess_communicate_utf8_async (p, NULL, NULL, on_list_done, g_object_ref (self));
}


/* ------------------------------------------------------------------ enroll */

static void read_enroll_line (MsDuoFpPanel *self);

static void
on_enroll_line (GObject *source, GAsyncResult *res, gpointer data)
{
  MsDuoFpPanel *self = MS_DUO_FP_PANEL (data);
  g_autofree char *line = g_data_input_stream_read_line_finish_utf8 (G_DATA_INPUT_STREAM (source), res, NULL, NULL);
  const char *p;

  if (!line) {                                        /* tool finished */
    g_clear_object (&self->enroll_out);
    g_clear_object (&self->enroll);
    refresh_list (self);
    g_object_unref (self);
    return;
  }
  g_strstrip (line);
  if ((p = strstr (line, "remaining "))) {
    g_autofree char *sub = g_strdup_printf ("Lift and touch again, moving the finger slightly · %s left",
                                            p + strlen ("remaining "));
    show_status (self, "Adding fingerprint…", sub, TRUE);
  } else if (g_str_has_prefix (line, "end enroll")) {
    gboolean ok = strstr (line, "end enroll   0 0") != NULL;
    show_status (self, ok ? "Fingerprint added" : "Adding the fingerprint failed",
                 ok ? "Saving…" : line, FALSE);
  } else if (g_str_has_prefix (line, "store db")) {
    gboolean ok = strstr (line, "store db     0 0") != NULL;
    show_status (self, ok ? "Fingerprint added" : "Could not save the fingerprint",
                 ok ? "You can now unlock with this finger" : line, FALSE);
  } else if (strstr (line, "image not usable")) {
    adw_action_row_set_subtitle (self->status_row, "Couldn't read that touch: try again");
  }
  read_enroll_line (self);
  g_object_unref (self);
}


static void
read_enroll_line (MsDuoFpPanel *self)
{
  g_data_input_stream_read_line_async (self->enroll_out, G_PRIORITY_DEFAULT, NULL,
                                       on_enroll_line, g_object_ref (self));
}


static void
on_add_activated (MsDuoFpPanel *self)
{
  g_autoptr (GError) err = NULL;

  if (self->busy)
    return;
  /* setsid: own process group, so Cancel can stop the whole tool (shell + python) */
  self->enroll = g_subprocess_new (G_SUBPROCESS_FLAGS_STDOUT_PIPE | G_SUBPROCESS_FLAGS_STDERR_MERGE, &err,
                                   "setsid", TOOL, "enroll", NULL);
  if (!self->enroll) {
    show_status (self, "Could not start", err->message, FALSE);
    return;
  }
  self->enroll_out = g_data_input_stream_new (g_subprocess_get_stdout_pipe (self->enroll));
  set_busy (self, TRUE);
  show_status (self, "Adding fingerprint…", "Touch the sensor on the right edge with the finger you want to add", TRUE);
  read_enroll_line (self);
}


static void
on_cancel_clicked (MsDuoFpPanel *self)
{
  const char *id;
  const char *restart[] = { TOOL, "restart", NULL };

  if (!self->enroll)
    return;
  id = g_subprocess_get_identifier (self->enroll);
  if (id)
    kill (-atoi (id), SIGTERM);
  g_spawn_async (NULL, (char **) restart, NULL, G_SPAWN_STDOUT_TO_DEV_NULL | G_SPAWN_STDERR_TO_DEV_NULL,
                 NULL, NULL, NULL, NULL);
  show_status (self, "Cancelled", NULL, FALSE);
}


/* ------------------------------------------------------------------ object */

static void
ms_duo_fp_panel_dispose (GObject *object)
{
  MsDuoFpPanel *self = MS_DUO_FP_PANEL (object);

  if (self->enroll)
    on_cancel_clicked (self);
  g_clear_object (&self->enroll_out);
  g_clear_object (&self->enroll);
  g_clear_pointer (&self->finger_rows, g_ptr_array_unref);
  G_OBJECT_CLASS (ms_duo_fp_panel_parent_class)->dispose (object);
}


static void
ms_duo_fp_panel_class_init (MsDuoFpPanelClass *klass)
{
  G_OBJECT_CLASS (klass)->dispose = ms_duo_fp_panel_dispose;
}


static void
ms_duo_fp_panel_init (MsDuoFpPanel *self)
{
  GtkWidget *page = adw_preferences_page_new ();
  GtkWidget *lock_group = adw_preferences_group_new ();
  GtkWidget *status_group = adw_preferences_group_new ();

  g_object_set (self, "title", "Surface Duo", NULL);
  self->finger_rows = g_ptr_array_new ();

  adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (lock_group), "Unlocking");
  adw_preferences_group_set_description (ADW_PREFERENCES_GROUP (lock_group),
                                         "Your PIN (the root password) always unlocks the phone.");

  self->unlock_row = ADW_SWITCH_ROW (adw_switch_row_new ());
  adw_preferences_row_set_title (ADW_PREFERENCES_ROW (self->unlock_row), "Unlock with fingerprint");
  adw_action_row_set_subtitle (ADW_ACTION_ROW (self->unlock_row), "Touch the sensor on the lock screen");
  adw_switch_row_set_active (self->unlock_row, !g_file_test (UNLOCK_OFF, G_FILE_TEST_EXISTS));
  g_signal_connect_swapped (self->unlock_row, "notify::active", G_CALLBACK (on_unlock_toggled), self);
  adw_preferences_group_add (ADW_PREFERENCES_GROUP (lock_group), GTK_WIDGET (self->unlock_row));

  self->autolock_row = ADW_SWITCH_ROW (adw_switch_row_new ());
  adw_preferences_row_set_title (ADW_PREFERENCES_ROW (self->autolock_row), "Lock when the screen turns off");
  adw_switch_row_set_active (self->autolock_row, g_file_test (AUTOLOCK, G_FILE_TEST_EXISTS));
  g_signal_connect_swapped (self->autolock_row, "notify::active", G_CALLBACK (on_autolock_toggled), self);
  adw_preferences_group_add (ADW_PREFERENCES_GROUP (lock_group), GTK_WIDGET (self->autolock_row));

  /* Display: automatic brightness (duo-sensorproxy follows this GNOME key with the light sensor) */
  {
    GtkWidget *display_group = adw_preferences_group_new ();
    GtkWidget *auto_row = adw_switch_row_new ();
    g_autoptr (GSettings) power = g_settings_new ("org.gnome.settings-daemon.plugins.power");

    adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (display_group), "Display");
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (auto_row), "Automatic brightness");
    adw_action_row_set_subtitle (ADW_ACTION_ROW (auto_row), "Adjust to the light sensor; the slider sets your preference");
    g_settings_bind (power, "ambient-enabled", auto_row, "active", G_SETTINGS_BIND_DEFAULT);
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (display_group), auto_row);
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), ADW_PREFERENCES_GROUP (display_group));
  }

  self->fingers_group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
  adw_preferences_group_set_title (self->fingers_group, "Fingerprints");
  self->add_row = ADW_BUTTON_ROW (adw_button_row_new ());
  adw_preferences_row_set_title (ADW_PREFERENCES_ROW (self->add_row), "Add fingerprint");
  adw_button_row_set_start_icon_name (self->add_row, "list-add-symbolic");
  g_signal_connect_swapped (self->add_row, "activated", G_CALLBACK (on_add_activated), self);
  adw_preferences_group_add (self->fingers_group, GTK_WIDGET (self->add_row));

  self->status_row = ADW_ACTION_ROW (adw_action_row_new ());
  self->cancel_btn = GTK_BUTTON (gtk_button_new_with_label ("Cancel"));
  gtk_widget_set_valign (GTK_WIDGET (self->cancel_btn), GTK_ALIGN_CENTER);
  g_signal_connect_swapped (self->cancel_btn, "clicked", G_CALLBACK (on_cancel_clicked), self);
  adw_action_row_add_suffix (self->status_row, GTK_WIDGET (self->cancel_btn));
  adw_preferences_group_add (ADW_PREFERENCES_GROUP (status_group), GTK_WIDGET (self->status_row));
  show_status (self, NULL, NULL, FALSE);

  adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), ADW_PREFERENCES_GROUP (lock_group));
  adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), self->fingers_group);
  adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), ADW_PREFERENCES_GROUP (status_group));
  adw_bin_set_child (ADW_BIN (self), page);

  refresh_list (self);
}


/* ------------------------------------------------------------------ GIO module */

char **g_io_ms_plugin_duo_fingerprint_query (void);

void
g_io_module_load (GIOModule *module)
{
  g_type_module_use (G_TYPE_MODULE (module));
  g_io_extension_point_implement (MS_EXTENSION_POINT_DEVICE_PANEL, MS_TYPE_DUO_FP_PANEL,
                                  "device-panel-duo-fingerprint", 10);
}

void
g_io_module_unload (GIOModule *module)
{
}

char **
g_io_ms_plugin_duo_fingerprint_query (void)
{
  char *extension_points[] = { MS_EXTENSION_POINT_DEVICE_PANEL, NULL };

  return g_strdupv (extension_points);
}
