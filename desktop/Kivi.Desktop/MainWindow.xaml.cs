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

    private void Nav_Click(object sender, RoutedEventArgs e)
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
        PlaceholderView.Visibility = tag == "chat" ? Visibility.Collapsed : Visibility.Visible;
        PlaceholderTitle.Text = PageTitle.Text;
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
