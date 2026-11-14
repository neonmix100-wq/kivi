using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Net.Http.Json;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;

namespace Kivi.Desktop;

public partial class MainWindow : Window
{
    private static readonly HttpClient Http = new() { Timeout = TimeSpan.FromMinutes(5) };
    private string _baseUrl = "http://127.0.0.1:8765";
    private bool _sending;
    private Process? _ownedBackend;

    public MainWindow()
    {
        InitializeComponent();
        Loaded += OnLoaded;
        Closed += (_, _) =>
        {
            if (_ownedBackend is { HasExited: false }) _ownedBackend.Kill(entireProcessTree: true);
        };
    }

    private async void OnLoaded(object sender, RoutedEventArgs e)
    {
        var found = await FindApiAsync();
        if (!found)
        {
            ConnectionText.Text = "Запускаю локальный сервер…";
            await StartBackendAsync();
            found = await FindApiAsync(40);
        }
        ConnectionText.Text = found
            ? $"API: {_baseUrl.Replace("http://", "")}"
            : "API недоступен";
        if (found) await LoadHistoryAsync();
    }

    private async Task<bool> FindApiAsync(int attempts = 1)
    {
        for (var attempt = 0; attempt < attempts; attempt++)
        {
            foreach (var port in new[] { 8765, 8766, 8767 })
            {
                var candidate = $"http://127.0.0.1:{port}";
                try
                {
                    using var response = await Http.GetAsync($"{candidate}/health");
                    if (!response.IsSuccessStatusCode) continue;
                    _baseUrl = candidate;
                    return true;
                }
                catch
                {
                    // try next port / attempt
                }
            }
            if (attempt + 1 < attempts) await Task.Delay(500);
        }
        return false;
    }

    private async Task StartBackendAsync()
    {
        var root = FindProjectRoot();
        if (root is null) return;
        var pythonw = Path.Combine(root, ".venv", "Scripts", "pythonw.exe");
        if (!File.Exists(pythonw)) return;
        try
        {
            _ownedBackend = Process.Start(new ProcessStartInfo
            {
                FileName = pythonw,
                Arguments = "-m kivi ui --no-browser --port 8765",
                WorkingDirectory = root,
                UseShellExecute = false,
                CreateNoWindow = true,
            });
        }
        catch
        {
            _ownedBackend = null;
        }
        await Task.CompletedTask;
    }

    private static string? FindProjectRoot()
    {
        foreach (var start in new[] { AppContext.BaseDirectory, Environment.CurrentDirectory })
        {
            var current = new DirectoryInfo(start);
            while (current is not null)
            {
                if (File.Exists(Path.Combine(current.FullName, ".venv", "Scripts", "pythonw.exe")))
                    return current.FullName;
                current = current.Parent;
            }
        }
        return null;
    }

    private async void Nav_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button button) return;
        var tag = button.Tag?.ToString() ?? "chat";
        var labels = new Dictionary<string, string>
        {
            ["chat"] = "Чат",
            ["voices"] = "Голоса",
            ["media"] = "Медиа",
            ["files"] = "Файлы",
            ["crm"] = "CRM",
            ["settings"] = "Настройки",
        };
        PageTitle.Text = labels.GetValueOrDefault(tag, "Kivi");
        ChatView.Visibility = tag == "chat" ? Visibility.Visible : Visibility.Collapsed;
        PageView.Visibility = tag == "chat" ? Visibility.Collapsed : Visibility.Visible;
        if (tag != "chat") await ShowPageAsync(tag);
    }

    private async Task LoadHistoryAsync()
    {
        using var doc = await GetJsonAsync("/history?limit=50");
        if (doc is null) return;
        if (!doc.RootElement.TryGetProperty("messages", out var messages)) return;
        MessagesPanel.Children.Clear();
        MessagesPanel.Children.Add(new TextBlock
        {
            Text = "ИСТОРИЯ",
            Foreground = (Brush)FindResource("Muted"),
            FontSize = 11,
            HorizontalAlignment = HorizontalAlignment.Center,
            Margin = new Thickness(0, 4, 0, 22),
        });
        foreach (var message in messages.EnumerateArray())
        {
            var role = message.TryGetProperty("role", out var r) ? r.GetString() : "";
            var content = message.TryGetProperty("content", out var c) ? c.GetString() : "";
            if (!string.IsNullOrWhiteSpace(content)) AddBubble(content!, role == "user");
        }
    }

    private async Task<JsonDocument?> GetJsonAsync(string path)
    {
        try
        {
            using var response = await Http.GetAsync(_baseUrl + path);
            var body = await response.Content.ReadAsStringAsync();
            if (!response.IsSuccessStatusCode) return null;
            return JsonDocument.Parse(body);
        }
        catch
        {
            return null;
        }
    }

    private async Task<JsonDocument?> PostJsonAsync(string path, object payload)
    {
        try
        {
            using var response = await Http.PostAsJsonAsync(_baseUrl + path, payload);
            var body = await response.Content.ReadAsStringAsync();
            if (!response.IsSuccessStatusCode) return null;
            return JsonDocument.Parse(body);
        }
        catch
        {
            return null;
        }
    }

    private async Task ShowPageAsync(string tag)
    {
        PagePanel.Children.Clear();
        switch (tag)
        {
            case "voices": await BuildVoicesPageAsync(); break;
            case "settings": await BuildSettingsPageAsync(); break;
            case "media": BuildMediaPage(); break;
            case "files": await BuildFilesPageAsync(); break;
            case "crm": await BuildCrmPageAsync(); break;
        }
    }

    private TextBlock PageHeading(string text) => new()
    {
        Text = text,
        Foreground = (Brush)FindResource("Text"),
        FontSize = 24,
        FontWeight = FontWeights.SemiBold,
        Margin = new Thickness(0, 0, 0, 6),
    };

    private TextBlock PageSub(string text) => new()
    {
        Text = text,
        Foreground = (Brush)FindResource("Muted"),
        FontSize = 13,
        TextWrapping = TextWrapping.Wrap,
        Margin = new Thickness(0, 0, 0, 20),
    };

    private Button PageButton(string text, RoutedEventHandler click)
    {
        var button = new Button
        {
            Content = text,
            Style = (Style)FindResource("NavButton"),
            HorizontalAlignment = HorizontalAlignment.Left,
            Margin = new Thickness(0, 0, 8, 8),
        };
        button.Click += click;
        return button;
    }

    private async Task BuildVoicesPageAsync()
    {
        PagePanel.Children.Add(PageHeading("Голоса"));
        PagePanel.Children.Add(PageSub("Выбери движок озвучивания. Активный голос хранится на сервере."));
        using var doc = await GetJsonAsync("/voices");
        if (doc is null)
        {
            PagePanel.Children.Add(PageSub("Не удалось загрузить голоса."));
            return;
        }
        if (!doc.RootElement.TryGetProperty("voices", out var voices)) return;
        foreach (var voice in voices.EnumerateArray())
        {
            var id = voice.TryGetProperty("id", out var i) ? i.GetString() ?? "" : "";
            var name = voice.TryGetProperty("name", out var n) ? n.GetString() ?? id : id;
            var engine = voice.TryGetProperty("engine", out var e) ? e.GetString() ?? "" : "";
            var active = voice.TryGetProperty("active", out var a) && a.GetBoolean();
            var available = !voice.TryGetProperty("available", out var av) || av.GetBoolean();
            var card = new Border
            {
                Background = (Brush)FindResource("Panel2"),
                BorderBrush = (Brush)FindResource("Line"),
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(12),
                Padding = new Thickness(16, 12, 16, 12),
                Margin = new Thickness(0, 0, 0, 10),
            };
            var row = new DockPanel();
            var info = new StackPanel();
            info.Children.Add(new TextBlock { Text = name, Foreground = (Brush)FindResource("Text"), FontSize = 15 });
            info.Children.Add(new TextBlock
            {
                Text = engine,
                Foreground = (Brush)FindResource("Muted"),
                FontSize = 11,
                Margin = new Thickness(0, 3, 0, 0),
            });
            row.Children.Add(info);
            var choose = PageButton(active ? "Активен" : available ? "Выбрать" : "Недоступен", async (_, _) =>
            {
                if (!available) return;
                using var result = await PostJsonAsync("/voices/active", new { id });
                await ShowPageAsync("voices");
            });
            choose.IsEnabled = available && !active;
            DockPanel.SetDock(choose, Dock.Right);
            row.Children.Add(choose);
            card.Child = row;
            PagePanel.Children.Add(card);
        }
    }

    private async Task BuildSettingsPageAsync()
    {
        PagePanel.Children.Add(PageHeading("Настройки"));
        PagePanel.Children.Add(PageSub("Голосовой движок и параметры локального сервера."));
        using var doc = await GetJsonAsync("/settings");
        if (doc is null)
        {
            PagePanel.Children.Add(PageSub("Не удалось загрузить настройки."));
            return;
        }
        var providers = new[] { "qwen", "silero", "piper", "fish" };
        var combo = new ComboBox
        {
            Width = 260,
            HorizontalAlignment = HorizontalAlignment.Left,
            Margin = new Thickness(0, 0, 0, 12),
        };
        foreach (var provider in providers) combo.Items.Add(provider);
        var current = doc.RootElement.TryGetProperty("voice_provider", out var p) ? p.GetString() : "qwen";
        combo.SelectedItem = providers.Contains(current) ? current : "qwen";
        PagePanel.Children.Add(combo);
        PagePanel.Children.Add(PageButton("Сохранить", async (_, _) =>
        {
            using var result = await PostJsonAsync("/settings", new { voice_provider = combo.SelectedItem?.ToString() });
            await ShowPageAsync("settings");
        }));
    }

    private void BuildMediaPage()
    {
        PagePanel.Children.Add(PageHeading("Медиа"));
        PagePanel.Children.Add(PageSub("Локальная генерация изображений через текущий Kivi API."));
        var prompt = new TextBox
        {
            MinHeight = 58,
            TextWrapping = TextWrapping.Wrap,
            AcceptsReturn = true,
            Background = (Brush)FindResource("Panel2"),
            Foreground = (Brush)FindResource("Text"),
            BorderBrush = (Brush)FindResource("Line"),
            Padding = new Thickness(10),
            Margin = new Thickness(0, 0, 0, 10),
        };
        PagePanel.Children.Add(prompt);
        var output = new StackPanel();
        PagePanel.Children.Add(PageButton("Сгенерировать изображение", async (_, _) =>
        {
            output.Children.Clear();
            output.Children.Add(PageSub("Генерирую…"));
            using var result = await PostJsonAsync("/media/image", new
            {
                prompt = prompt.Text,
                width = 512,
                height = 512,
                model = "flux",
            });
            output.Children.Clear();
            if (result is null)
            {
                output.Children.Add(PageSub("Генерация недоступна."));
                return;
            }
            var image = result.RootElement.TryGetProperty("image", out var value) ? value.GetString() : null;
            if (image is not null && image.StartsWith("data:image/", StringComparison.Ordinal))
            {
                var comma = image.IndexOf(',');
                if (comma > 0) output.Children.Add(BuildImage(Convert.FromBase64String(image[(comma + 1)..])));
            }
        }));
        PagePanel.Children.Add(output);
    }

    private static Image BuildImage(byte[] bytes)
    {
        var bitmap = new System.Windows.Media.Imaging.BitmapImage();
        using var stream = new MemoryStream(bytes);
        bitmap.BeginInit();
        bitmap.CacheOption = System.Windows.Media.Imaging.BitmapCacheOption.OnLoad;
        bitmap.StreamSource = stream;
        bitmap.EndInit();
        bitmap.Freeze();
        return new Image
        {
            Source = bitmap,
            MaxWidth = 420,
            Stretch = Stretch.Uniform,
            HorizontalAlignment = HorizontalAlignment.Left,
            Margin = new Thickness(0, 12, 0, 0),
        };
    }

    private async Task BuildFilesPageAsync()
    {
        PagePanel.Children.Add(PageHeading("Файлы"));
        PagePanel.Children.Add(PageSub("Нативный список файлов через /fs/list. Рабочая папка Kivi."));
        var list = new StackPanel();
        using var doc = await GetJsonAsync("/fs/list?path=.");
        if (doc is null)
        {
            PagePanel.Children.Add(PageSub("Не удалось получить список файлов."));
            return;
        }
        if (doc.RootElement.TryGetProperty("entries", out var entries))
        {
            foreach (var entry in entries.EnumerateArray())
            {
                var name = entry.TryGetProperty("name", out var n) ? n.GetString() : "";
                var type = entry.TryGetProperty("type", out var t) ? t.GetString() : "file";
                var card = new Border
                {
                    Background = (Brush)FindResource("Panel2"),
                    BorderBrush = (Brush)FindResource("Line"),
                    BorderThickness = new Thickness(1),
                    CornerRadius = new CornerRadius(9),
                    Padding = new Thickness(12, 9, 12, 9),
                    Margin = new Thickness(0, 0, 0, 6),
                };
                card.Child = new TextBlock
                {
                    Text = $"{(type == "dir" ? "▸" : "·")} {name}",
                    Foreground = (Brush)FindResource("Text"),
                    FontSize = 14,
                };
                list.Children.Add(card);
            }
        }
        PagePanel.Children.Add(list);
    }

    private async Task BuildCrmPageAsync()
    {
        PagePanel.Children.Add(PageHeading("CRM"));
        PagePanel.Children.Add(PageSub("Статус расширения AltCRM и read-only карточка."));
        using var doc = await GetJsonAsync("/crm/status");
        var connected = doc?.RootElement.TryGetProperty("connected", out var c) == true && c.GetBoolean();
        PagePanel.Children.Add(PageSub(connected ? "Расширение подключено." : "Расширение не подключено."));
        var read = PageButton("Прочитать заказ", async (_, _) =>
        {
            using var result = await GetJsonAsync("/crm/read");
            if (result is null) return;
            PagePanel.Children.Add(PageSub(result.RootElement.ToString()));
        });
        PagePanel.Children.Add(read);
    }

    private void ComposerInput_KeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key == Key.Enter && Keyboard.Modifiers != ModifierKeys.Shift)
        {
            e.Handled = true;
            _ = SendMessageAsync();
        }
    }

    private void Send_Click(object sender, RoutedEventArgs e) => _ = SendMessageAsync();

    private async Task SendMessageAsync()
    {
        if (_sending) return;
        var text = ComposerInput.Text.Trim();
        if (text.Length == 0) return;
        _sending = true;
        ComposerInput.Clear();
        AddBubble(text, fromUser: true);
        AddBubble("…", fromUser: false, pending: true);
        try
        {
            using var response = await Http.PostAsJsonAsync($"{_baseUrl}/chat", new { message = text });
            var body = await response.Content.ReadAsStringAsync();
            if (!response.IsSuccessStatusCode)
            {
                ReplacePending($"Ошибка сервера: HTTP {(int)response.StatusCode}");
                return;
            }
            using var doc = JsonDocument.Parse(body);
            var answer = doc.RootElement.TryGetProperty("answer", out var value)
                ? value.GetString()
                : null;
            ReplacePending(string.IsNullOrWhiteSpace(answer) ? "Пустой ответ." : answer);
        }
        catch (Exception ex)
        {
            ReplacePending($"Не удалось связаться с Kivi: {ex.Message}");
        }
        finally
        {
            _sending = false;
        }
    }

    private Border? _pendingBubble;

    private void AddBubble(string text, bool fromUser, bool pending = false)
    {
        var content = new TextBlock
        {
            Text = text,
            Foreground = (Brush)FindResource("Text"),
            FontSize = 14,
            TextWrapping = TextWrapping.Wrap,
        };
        var border = new Border
        {
            Background = new SolidColorBrush(fromUser ? Color.FromRgb(10, 58, 52) : Color.FromRgb(8, 47, 43)),
            BorderBrush = (Brush)FindResource("Line"),
            BorderThickness = fromUser ? new Thickness(0) : new Thickness(1),
            CornerRadius = new CornerRadius(fromUser ? 15 : 4, 15, 15, 15),
            Padding = new Thickness(16, 12, 16, 12),
            Margin = new Thickness(fromUser ? 120 : 0, 0, fromUser ? 0 : 120, 18),
            HorizontalAlignment = fromUser ? HorizontalAlignment.Right : HorizontalAlignment.Left,
            MaxWidth = 620,
            Child = content,
        };
        MessagesPanel.Children.Add(border);
        if (pending) _pendingBubble = border;
        MessagesScroll.ScrollToEnd();
    }

    private void ReplacePending(string text)
    {
        if (_pendingBubble?.Child is TextBlock block)
        {
            block.Text = text;
            _pendingBubble = null;
        }
        MessagesScroll.ScrollToEnd();
    }
}
